import os
import shutil
import zipfile
import socket
import json
import hashlib
import threading
import time
import sqlite3
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory, Response
from werkzeug.utils import secure_filename
import queue
import uuid

app = Flask(__name__)

# Configuration avancée
UPLOAD_FOLDER = 'shared_files'
TEMP_FOLDER = os.path.join(UPLOAD_FOLDER, '.temp')
DB_FILE = 'file_server.db'
CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB chunks pour meilleure stabilité
MAX_CONCURRENT_UPLOADS = 3
RESUME_TIMEOUT = 3600  # 1 heure pour reprendre un upload

# Configuration Flask optimisée
app.config['MAX_CONTENT_LENGTH'] = None
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# Créer les dossiers nécessaires
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# Base de données pour le suivi des transferts
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Table pour les uploads en cours
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS uploads (
            id TEXT PRIMARY KEY,
            filename TEXT,
            total_size INTEGER,
            uploaded_size INTEGER,
            total_chunks INTEGER,
            uploaded_chunks INTEGER,
            status TEXT,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            path TEXT,
            relative_path TEXT
        )
    ''')
    
    # Table pour l'historique
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            filename TEXT,
            size INTEGER,
            ip_address TEXT,
            timestamp TIMESTAMP
        )
    ''')
    
    # Table pour les favoris
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE,
            name TEXT,
            created_at TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# Gestionnaires globaux
upload_queue = queue.Queue()
active_uploads = {}
active_downloads = {}

class UploadManager:
    def __init__(self):
        self.active_uploads = {}
        self.upload_lock = threading.Lock()
    
    def start_upload(self, upload_id, filename, total_size, total_chunks, path, relative_path=""):
        with self.upload_lock:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO uploads 
                (id, filename, total_size, uploaded_size, total_chunks, uploaded_chunks, status, created_at, updated_at, path, relative_path)
                VALUES (?, ?, ?, 0, ?, 0, 'active', ?, ?, ?, ?)
            ''', (upload_id, filename, total_size, total_chunks, datetime.now(), datetime.now(), path, relative_path))
            
            conn.commit()
            conn.close()
    
    def update_chunk(self, upload_id, chunk_size):
        with self.upload_lock:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            cursor.execute('''
                UPDATE uploads 
                SET uploaded_size = uploaded_size + ?, uploaded_chunks = uploaded_chunks + 1, updated_at = ?
                WHERE id = ?
            ''', (chunk_size, datetime.now(), upload_id))
            
            conn.commit()
            conn.close()
    
    def complete_upload(self, upload_id):
        with self.upload_lock:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            cursor.execute('UPDATE uploads SET status = "completed" WHERE id = ?', (upload_id,))
            conn.commit()
            conn.close()
    
    def get_upload_status(self, upload_id):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM uploads WHERE id = ?', (upload_id,))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return {
                'id': result[0],
                'filename': result[1],
                'total_size': result[2],
                'uploaded_size': result[3],
                'total_chunks': result[4],
                'uploaded_chunks': result[5],
                'status': result[6],
                'progress': (result[3] / result[2]) * 100 if result[2] > 0 else 0
            }
        return None

upload_manager = UploadManager()

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "localhost"

def get_directory_size(path):
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                if os.path.exists(filepath):
                    total += os.path.getsize(filepath)
    except:
        pass
    return total

def format_size(bytes_size):
    if bytes_size == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} PB"

def get_file_hash(filepath):
    hash_md5 = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except:
        return None

def add_to_history(action, filename, size, ip_address):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO history (action, filename, size, ip_address, timestamp)
        VALUES (?, ?, ?, ?, ?)
    ''', (action, filename, size, ip_address, datetime.now()))
    
    conn.commit()
    conn.close()

def get_storage_info():
    used = get_directory_size(UPLOAD_FOLDER)
    total, used_disk, free = shutil.disk_usage(UPLOAD_FOLDER)
    
    return {
        'used': used,
        'free': free,
        'total': total,
        'used_formatted': format_size(used),
        'free_formatted': format_size(free),
        'total_formatted': format_size(total),
        'percentage': (used_disk / total) * 100 if total > 0 else 0
    }

def get_file_list(directory="", sort_by="name", sort_order="asc"):
    full_path = os.path.join(UPLOAD_FOLDER, directory)
    if not os.path.exists(full_path):
        return []
    
    items = []
    for item in os.listdir(full_path):
        item_path = os.path.join(full_path, item)
        relative_path = os.path.join(directory, item) if directory else item
        
        try:
            stats = os.stat(item_path)
            if os.path.isdir(item_path):
                size = get_directory_size(item_path)
                file_count = len([f for f in os.listdir(item_path) if os.path.isfile(os.path.join(item_path, f))])
                items.append({
                    'name': item,
                    'path': relative_path,
                    'type': 'directory',
                    'size': size,
                    'size_formatted': format_size(size),
                    'file_count': file_count,
                    'modified': datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'created': datetime.fromtimestamp(stats.st_ctime).strftime('%Y-%m-%d %H:%M:%S')
                })
            else:
                size = stats.st_size
                items.append({
                    'name': item,
                    'path': relative_path,
                    'type': 'file',
                    'size': size,
                    'size_formatted': format_size(size),
                    'modified': datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'created': datetime.fromtimestamp(stats.st_ctime).strftime('%Y-%m-%d %H:%M:%S'),
                    'hash': get_file_hash(item_path)
                })
        except:
            continue
    
    # Tri
    reverse = sort_order == "desc"
    if sort_by == "size":
        items.sort(key=lambda x: x['size'], reverse=reverse)
    elif sort_by == "modified":
        items.sort(key=lambda x: x['modified'], reverse=reverse)
    else:
        items.sort(key=lambda x: (x['type'] == 'file', x['name'].lower()), reverse=reverse)
    
    return items

@app.route('/')
def index():
    html = '''
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚀 Serveur de Partage WiFi Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
        }
        .container { 
            max-width: 1400px; 
            margin: 0 auto; 
            padding: 20px;
            position: relative;
        }
        .header { 
            background: rgba(255, 255, 255, 0.1); 
            backdrop-filter: blur(10px);
            border-radius: 20px;
            color: white; 
            padding: 30px; 
            text-align: center; 
            margin-bottom: 30px;
            border: 1px solid rgba(255, 255, 255, 0.2);
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 15px;
            padding: 25px;
            text-align: center;
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.2);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }
        .stat-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 15px 45px rgba(0,0,0,0.2);
        }
        .stat-number { font-size: 2.5em; font-weight: bold; color: #667eea; margin-bottom: 10px; }
        .stat-label { color: #666; font-size: 1.1em; }
        .progress-ring {
            width: 80px;
            height: 80px;
            margin: 0 auto 15px;
        }
        .progress-ring circle {
            fill: none;
            stroke-width: 8;
        }
        .progress-bg { stroke: #e0e0e0; }
        .progress-fill { 
            stroke: #667eea; 
            stroke-linecap: round;
            transition: stroke-dasharray 0.3s ease;
        }
        .upload-section {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 30px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.2);
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        }
        .upload-area {
            border: 3px dashed #ccc;
            border-radius: 15px;
            padding: 50px;
            text-align: center;
            margin: 20px 0;
            cursor: pointer;
            transition: all 0.3s ease;
            background: linear-gradient(45deg, #f8f9ff, #fff);
            position: relative;
            overflow: hidden;
        }
        .upload-area:before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(102, 126, 234, 0.1), transparent);
            transition: left 0.5s;
        }
        .upload-area:hover:before {
            left: 100%;
        }
        .upload-area:hover {
            border-color: #667eea;
            background: linear-gradient(45deg, #f0f4ff, #fff);
            transform: scale(1.02);
        }
        .upload-area.dragover {
            border-color: #667eea;
            background: linear-gradient(45deg, #e8f2ff, #f0f8ff);
            transform: scale(1.05);
            box-shadow: 0 10px 30px rgba(102, 126, 234, 0.3);
        }
        .upload-buttons {
            display: flex;
            gap: 15px;
            justify-content: center;
            flex-wrap: wrap;
            margin: 20px 0;
        }
        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            font-size: 1em;
            font-weight: 600;
            transition: all 0.3s ease;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        }
        .btn:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,0,0,0.2); }
        .btn-primary { background: linear-gradient(135deg, #667eea, #764ba2); color: white; }
        .btn-success { background: linear-gradient(135deg, #4CAF50, #45a049); color: white; }
        .btn-warning { background: linear-gradient(135deg, #FF9800, #F57C00); color: white; }
        .btn-danger { background: linear-gradient(135deg, #f44336, #d32f2f); color: white; }
        .btn-info { background: linear-gradient(135deg, #2196F3, #1976D2); color: white; }
        .file-list {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 30px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.2);
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        }
        .search-sort-bar {
            display: flex;
            gap: 15px;
            margin-bottom: 25px;
            flex-wrap: wrap;
            align-items: center;
        }
        .search-box {
            flex: 1;
            min-width: 250px;
            padding: 12px 20px;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 1em;
            transition: border-color 0.3s;
        }
        .search-box:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        .sort-controls {
            display: flex;
            gap: 10px;
            align-items: center;
        }
        .select-box {
            padding: 8px 15px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            background: white;
            cursor: pointer;
        }
        .file-item {
            display: flex;
            align-items: center;
            padding: 20px;
            border-bottom: 1px solid #eee;
            transition: all 0.3s ease;
            border-radius: 10px;
            margin-bottom: 5px;
        }
        .file-item:hover {
            background: linear-gradient(45deg, #f8f9ff, #fff);
            transform: translateX(5px);
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        }
        .file-icon {
            width: 50px;
            height: 50px;
            margin-right: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 12px;
            font-size: 1.5em;
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        }
        .folder-icon { background: linear-gradient(135deg, #FFC107, #FF8F00); color: white; }
        .file-icon-default { background: linear-gradient(135deg, #2196F3, #1976D2); color: white; }
        .file-info {
            flex-grow: 1;
            min-width: 0;
        }
        .file-name {
            font-weight: 600;
            margin-bottom: 8px;
            font-size: 1.1em;
            color: #333;
            word-break: break-word;
        }
        .file-details {
            color: #666;
            font-size: 0.9em;
            line-height: 1.4;
        }
        .file-actions {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }
        .progress-container {
            margin: 25px 0;
            padding: 25px;
            background: linear-gradient(45deg, #f8f9ff, #fff);
            border-radius: 15px;
            border: 2px solid #e8f2ff;
        }
        .progress {
            background: #e0e0e0;
            border-radius: 15px;
            height: 30px;
            overflow: hidden;
            margin: 15px 0;
            box-shadow: inset 0 2px 5px rgba(0,0,0,0.1);
        }
        .progress-bar {
            background: linear-gradient(90deg, #4CAF50, #45a049);
            height: 100%;
            width: 0%;
            transition: width 0.3s ease;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: bold;
            position: relative;
            overflow: hidden;
        }
        .progress-bar:before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.3), transparent);
            animation: shimmer 2s infinite;
        }
        @keyframes shimmer {
            0% { left: -100%; }
            100% { left: 100%; }
        }
        .hidden { display: none !important; }
        .breadcrumb {
            margin-bottom: 25px;
            padding: 15px;
            background: linear-gradient(45deg, #f0f4ff, #fff);
            border-radius: 10px;
            border: 1px solid #e8f2ff;
        }
        .breadcrumb a {
            color: #667eea;
            text-decoration: none;
            margin-right: 15px;
            padding: 5px 10px;
            border-radius: 5px;
            transition: background 0.3s;
        }
        .breadcrumb a:hover {
            background: rgba(102, 126, 234, 0.1);
        }
        .floating-panel {
            position: fixed;
            top: 20px;
            right: 20px;
            background: rgba(255, 255, 255, 0.95);
            border-radius: 15px;
            padding: 20px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.2);
            box-shadow: 0 8px 32px rgba(0,0,0,0.2);
            z-index: 1000;
            min-width: 300px;
        }
        .notification {
            padding: 15px 20px;
            margin: 10px 0;
            border-radius: 10px;
            color: white;
            font-weight: 500;
            animation: slideIn 0.3s ease;
        }
        .notification.success { background: linear-gradient(135deg, #4CAF50, #45a049); }
        .notification.error { background: linear-gradient(135deg, #f44336, #d32f2f); }
        .notification.info { background: linear-gradient(135deg, #2196F3, #1976D2); }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        .multi-upload-queue {
            max-height: 400px;
            overflow-y: auto;
            margin: 20px 0;
        }
        .queue-item {
            background: #f8f9fa;
            border: 1px solid #e9ecef;
            border-radius: 8px;
            padding: 15px;
            margin: 10px 0;
        }
        .speed-indicator {
            font-size: 0.9em;
            color: #28a745;
            margin-left: 10px;
        }
        @media (max-width: 768px) {
            .container { padding: 10px; }
            .stats-grid { grid-template-columns: 1fr; }
            .upload-buttons { flex-direction: column; align-items: center; }
            .search-sort-bar { flex-direction: column; }
            .file-actions { flex-direction: column; }
            .floating-panel { position: relative; top: auto; right: auto; margin: 20px 0; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🚀 Serveur de Partage WiFi Pro</h1>
            <p>IP: <strong id="server-ip"></strong> | Statut: <span id="server-status">🟢 En ligne</span></p>
        </div>
        
        <!-- Statistiques -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-number" id="total-files">0</div>
                <div class="stat-label">📁 Fichiers</div>
            </div>
            <div class="stat-card">
                <div class="stat-number" id="total-size">0 B</div>
                <div class="stat-label">💾 Taille totale</div>
            </div>
            <div class="stat-card">
                <div class="stat-number" id="active-transfers">0</div>
                <div class="stat-label">⚡ Transferts actifs</div>
            </div>
            <div class="stat-card">
                <svg class="progress-ring" id="storage-ring">
                    <circle class="progress-bg" cx="40" cy="40" r="36"></circle>
                    <circle class="progress-fill" cx="40" cy="40" r="36" 
                            stroke-dasharray="0 226" transform="rotate(-90 40 40)"></circle>
                </svg>
                <div class="stat-label">💽 Espace disque</div>
            </div>
        </div>
        
        <!-- Section upload -->
        <div class="upload-section">
            <h3>📤 Zone de téléversement</h3>
            <div class="upload-area" id="upload-area">
                <div style="position: relative; z-index: 1;">
                    <div style="font-size: 3em; margin-bottom: 20px;">☁️</div>
                    <p style="font-size: 1.3em; margin-bottom: 10px; font-weight: 600;">
                        Glissez vos fichiers ici ou cliquez pour sélectionner
                    </p>
                    <p style="color: #666; font-size: 1em;">
                        Fichiers individuels, dossiers complets - AUCUNE LIMITE
                    </p>
                </div>
            </div>
            
            <div class="upload-buttons">
                <button class="btn btn-primary" onclick="selectFiles()">
                    📄 Fichiers individuels
                </button>
                <button class="btn btn-primary" onclick="selectFolder()">
                    📁 Dossier complet
                </button>
                <button class="btn btn-warning" onclick="pauseAllUploads()">
                    ⏸️ Pause tous
                </button>
                <button class="btn btn-success" onclick="resumeAllUploads()">
                    ▶️ Reprendre tous
                </button>
                <button class="btn btn-danger" onclick="cancelAllUploads()">
                    ❌ Annuler tous
                </button>
            </div>
            
            <input type="file" id="file-input-folder" multiple webkitdirectory style="display: none;">
            <input type="file" id="file-input-files" multiple style="display: none;">
            
            <!-- Queue des uploads multiples -->
            <div class="multi-upload-queue" id="upload-queue"></div>
        </div>
        
        <!-- Navigation et recherche -->
        <div class="breadcrumb" id="breadcrumb"></div>
        
        <div class="file-list">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 25px;">
                <h3>📂 Explorateur de fichiers</h3>
                <div style="display: flex; gap: 10px;">
                    <button class="btn btn-info" onclick="showHistory()">📊 Historique</button>
                    <button class="btn btn-warning" onclick="showFavorites()">⭐ Favoris</button>
                    <button class="btn btn-success" onclick="refreshFiles()">🔄 Actualiser</button>
                </div>
            </div>
            
            <div class="search-sort-bar">
                <input type="text" class="search-box" id="search-box" placeholder="🔍 Rechercher des fichiers...">
                <div class="sort-controls">
                    <label>Trier par:</label>
                    <select class="select-box" id="sort-by">
                        <option value="name">Nom</option>
                        <option value="size">Taille</option>
                        <option value="modified">Date modifiée</option>
                    </select>
                    <select class="select-box" id="sort-order">
                        <option value="asc">Croissant</option>
                        <option value="desc">Décroissant</option>
                    </select>
                </div>
            </div>
            
            <div id="file-list-content"></div>
        </div>
    </div>
    
    <!-- Panel flottant pour les notifications -->
    <div class="floating-panel hidden" id="notification-panel">
        <h4>🔔 Notifications</h4>
        <div id="notifications"></div>
    </div>

    <script>
        // Variables globales
        let currentPath = '';
        let uploadQueue = [];
        let activeUploads = new Map();
        let serverStats = {};
        let notifications = [];
        
        // Initialisation
        document.addEventListener('DOMContentLoaded', function() {
            loadServerInfo();
            loadFiles();
            setupDragAndDrop();
            setupSearch();
            startStatsUpdater();
            setupKeyboardShortcuts();
        });
        
        function startStatsUpdater() {
            setInterval(() => {
                updateStats();
                cleanupOldNotifications();
            }, 5000);
        }
        
        function setupKeyboardShortcuts() {
            document.addEventListener('keydown', function(e) {
                if (e.ctrlKey || e.metaKey) {
                    switch(e.key) {
                        case 'u': 
                            e.preventDefault(); 
                            selectFiles(); 
                            break;
                        case 'f': 
                            e.preventDefault(); 
                            document.getElementById('search-box').focus(); 
                            break;
                        case 'r': 
                            e.preventDefault(); 
                            refreshFiles(); 
                            break;
                    }
                }
                if (e.key === 'Escape') {
                    hideNotificationPanel();
                }
            });
        }
        
        function loadServerInfo() {
            fetch('/api/info')
                .then(response => response.json())
                .then(data => {
                    document.getElementById('server-ip').textContent = data.ip;
                    updateStorageStats(data.storage);
                    serverStats = data;
                })
                .catch(() => {
                    showNotification('Erreur de connexion au serveur', 'error');
                    document.getElementById('server-status').innerHTML = '🔴 Hors ligne';
                });
        }
        
        function updateStorageStats(storage) {
            const ring = document.getElementById('storage-ring').querySelector('.progress-fill');
            const circumference = 2 * Math.PI * 36;
            const offset = circumference - (storage.percentage / 100) * circumference;
            ring.setAttribute('stroke-dasharray', `${circumference - offset} ${offset}`);
            
            document.getElementById('total-size').textContent = storage.used_formatted;
        }
        
        function updateStats() {
            fetch('/api/stats')
                .then(response => response.json())
                .then(data => {
                    document.getElementById('total-files').textContent = data.total_files;
                    document.getElementById('active-transfers').textContent = activeUploads.size;
                    updateStorageStats(data.storage);
                });
        }
        
        function loadFiles(path = '', sortBy = 'name', sortOrder = 'asc') {
            currentPath = path;
            
            fetch(`/api/files?path=${encodeURIComponent(path)}&sort_by=${sortBy}&sort_order=${sortOrder}`)
                .then(response => response.json())
                .then(data => {
                    updateBreadcrumb(path);
                    updateFileList(data.files);
                    updateStorageStats(data.storage);
                })
                .catch(() => showNotification('Erreur lors du chargement des fichiers', 'error'));
        }
        
        function refreshFiles() {
            const sortBy = document.getElementById('sort-by').value;
            const sortOrder = document.getElementById('sort-order').value;
            loadFiles(currentPath, sortBy, sortOrder);
            showNotification('Liste des fichiers actualisée', 'success');
        }
        
        function updateBreadcrumb(path) {
            const breadcrumb = document.getElementById('breadcrumb');
            let html = '<a href="#" onclick="loadFiles(\\'\\')">🏠 Racine</a>';
            
            if (path) {
                const parts = path.split('/');
                let currentPath = '';
                
                for (let part of parts) {
                    currentPath += (currentPath ? '/' : '') + part;
                    html += ` / <a href="#" onclick="loadFiles(\\'${currentPath}\\')">${part}</a>`;
                }
            }
            
            breadcrumb.innerHTML = html;
        }
        
        function updateFileList(files) {
            const container = document.getElementById('file-list-content');
            
            if (files.length === 0) {
                container.innerHTML = `
                    <div style="text-align: center; padding: 60px; color: #666;">
                        <div style="font-size: 4em; margin-bottom: 20px;">📭</div>
                        <h3>Aucun fichier trouvé</h3>
                        <p>Commencez par téléverser des fichiers ci-dessus</p>
                    </div>
                `;
                return;
            }
            
            let html = '';
            files.forEach(file => {
                const icon = file.type === 'directory' ? 
                    '<div class="file-icon folder-icon">📁</div>' : 
                    '<div class="file-icon file-icon-default">📄</div>';
                
                const extraInfo = file.type === 'directory' ? 
                    `${file.file_count} fichier(s)` : 
                    `Hash: ${file.hash ? file.hash.substring(0, 8) : 'N/A'}`;
                
                const actions = file.type === 'directory' ?
                    `<button class="btn btn-primary" onclick="loadFiles(\\'${file.path}\\')">📂 Ouvrir</button>
                     <button class="btn btn-success" onclick="downloadFile(\\'${file.path}\\', true)">📥 ZIP</button>
                     <button class="btn btn-warning" onclick="addToFavorites(\\'${file.path}\\', \\'${file.name}\\')">⭐ Favori</button>
                     <button class="btn btn-danger" onclick="deleteFile(\\'${file.path}\\')">🗑️ Suppr</button>` :
                    `<button class="btn btn-success" onclick="downloadFile(\\'${file.path}\\', false)">📥 Télécharger</button>
                     <button class="btn btn-info" onclick="previewFile(\\'${file.path}\\')">👁️ Aperçu</button>
                     <button class="btn btn-warning" onclick="addToFavorites(\\'${file.path}\\', \\'${file.name}\\')">⭐ Favori</button>
                     <button class="btn btn-danger" onclick="deleteFile(\\'${file.path}\\')">🗑️ Suppr</button>`;
                
                html += `
                    <div class="file-item">
                        ${icon}
                        <div class="file-info">
                            <div class="file-name">${file.name}</div>
                            <div class="file-details">
                                ${file.size_formatted} • ${file.modified}<br>
                                Créé: ${file.created} • ${extraInfo}
                            </div>
                        </div>
                        <div class="file-actions">
                            ${actions}
                        </div>
                    </div>
                `;
            });
            
            container.innerHTML = html;
        }
        
        function setupSearch() {
            const searchBox = document.getElementById('search-box');
            const sortBy = document.getElementById('sort-by');
            const sortOrder = document.getElementById('sort-order');
            
            let searchTimeout;
            searchBox.addEventListener('input', function() {
                clearTimeout(searchTimeout);
                searchTimeout = setTimeout(() => {
                    filterFiles(this.value);
                }, 300);
            });
            
            sortBy.addEventListener('change', () => loadFiles(currentPath, sortBy.value, sortOrder.value));
            sortOrder.addEventListener('change', () => loadFiles(currentPath, sortBy.value, sortOrder.value));
        }
        
        function filterFiles(searchTerm) {
            const items = document.querySelectorAll('.file-item');
            items.forEach(item => {
                const fileName = item.querySelector('.file-name').textContent.toLowerCase();
                const isVisible = fileName.includes(searchTerm.toLowerCase());
                item.style.display = isVisible ? 'flex' : 'none';
            });
        }
        
        function selectFiles() {
            document.getElementById('file-input-files').click();
        }
        
        function selectFolder() {
            document.getElementById('file-input-folder').click();
        }
        
        function setupDragAndDrop() {
            const uploadArea = document.getElementById('upload-area');
            
            ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
                uploadArea.addEventListener(eventName, preventDefaults, false);
                document.body.addEventListener(eventName, preventDefaults, false);
            });
            
            function preventDefaults(e) {
                e.preventDefault();
                e.stopPropagation();
            }
            
            ['dragenter', 'dragover'].forEach(eventName => {
                uploadArea.addEventListener(eventName, highlight, false);
            });
            
            ['dragleave', 'drop'].forEach(eventName => {
                uploadArea.addEventListener(eventName, unhighlight, false);
            });
            
            function highlight(e) {
                uploadArea.classList.add('dragover');
            }
            
            function unhighlight(e) {
                uploadArea.classList.remove('dragover');
            }
            
            uploadArea.addEventListener('drop', handleDrop, false);
            uploadArea.addEventListener('click', () => document.getElementById('file-input-files').click());
            
            function handleDrop(e) {
                const dt = e.dataTransfer;
                const files = dt.files;
                addFilesToQueue(Array.from(files));
            }
            
            // Gestion des inputs
            document.getElementById('file-input-folder').addEventListener('change', function(e) {
                addFilesToQueue(Array.from(e.target.files));
            });
            
            document.getElementById('file-input-files').addEventListener('change', function(e) {
                addFilesToQueue(Array.from(e.target.files));
            });
        }
        
        function addFilesToQueue(files) {
            files.forEach(file => {
                const uploadId = generateUUID();
                const queueItem = {
                    id: uploadId,
                    file: file,
                    status: 'queued',
                    progress: 0,
                    speed: 0,
                    startTime: null,
                    pauseTime: null
                };
                
                uploadQueue.push(queueItem);
                addQueueItemToDOM(queueItem);
            });
            
            processUploadQueue();
            showNotification(`${files.length} fichier(s) ajouté(s) à la queue`, 'info');
        }
        
        function addQueueItemToDOM(queueItem) {
            const queueContainer = document.getElementById('upload-queue');
            
            const queueItemDiv = document.createElement('div');
            queueItemDiv.className = 'queue-item';
            queueItemDiv.id = `queue-${queueItem.id}`;
            
            queueItemDiv.innerHTML = `
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <div class="file-name">${queueItem.file.name}</div>
                    <div class="file-size">${formatSize(queueItem.file.size)}</div>
                </div>
                <div class="progress">
                    <div class="progress-bar" id="progress-${queueItem.id}">0%</div>
                </div>
                <div style="display: flex; justify-content: between; align-items: center; font-size: 0.9em; color: #666;">
                    <span id="status-${queueItem.id}">En attente...</span>
                    <span class="speed-indicator" id="speed-${queueItem.id}"></span>
                </div>
                <div style="margin-top: 10px;">
                    <button class="btn btn-warning" onclick="pauseUpload('${queueItem.id}')" id="pause-${queueItem.id}">⏸️</button>
                    <button class="btn btn-success hidden" onclick="resumeUpload('${queueItem.id}')" id="resume-${queueItem.id}">▶️</button>
                    <button class="btn btn-danger" onclick="cancelUpload('${queueItem.id}')">❌</button>
                </div>
            `;
            
            queueContainer.appendChild(queueItemDiv);
        }
        
        async function processUploadQueue() {
            const activeCount = Array.from(activeUploads.values()).filter(u => u.status === 'active').length;
            
            if (activeCount >= 3) return; // Max 3 uploads simultanés
            
            const nextItem = uploadQueue.find(item => item.status === 'queued');
            if (!nextItem) return;
            
            nextItem.status = 'active';
            activeUploads.set(nextItem.id, nextItem);
            
            await uploadFileWithChunks(nextItem);
            
            // Continuer avec le suivant
            setTimeout(processUploadQueue, 1000);
        }
        
        async function uploadFileWithChunks(queueItem) {
            const file = queueItem.file;
            const uploadId = queueItem.id;
            const chunkSize = 2 * 1024 * 1024; // 2MB chunks
            const totalChunks = Math.ceil(file.size / chunkSize);
            
            updateQueueItemStatus(uploadId, 'Upload en cours...', 'info');
            queueItem.startTime = Date.now();
            
            try {
                for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex++) {
                    if (queueItem.status === 'paused') {
                        updateQueueItemStatus(uploadId, 'En pause', 'warning');
                        return;
                    }
                    
                    if (queueItem.status === 'cancelled') {
                        removeFromQueue(uploadId);
                        return;
                    }
                    
                    const start = chunkIndex * chunkSize;
                    const end = Math.min(start + chunkSize, file.size);
                    const chunk = file.slice(start, end);
                    
                    const formData = new FormData();
                    formData.append('chunk', chunk);
                    formData.append('fileName', file.webkitRelativePath || file.name);
                    formData.append('chunkIndex', chunkIndex);
                    formData.append('totalChunks', totalChunks);
                    formData.append('uploadId', uploadId);
                    formData.append('path', currentPath);
                    if (file.webkitRelativePath) {
                        formData.append('relativePath', file.webkitRelativePath);
                    }
                    
                    const response = await fetch('/api/upload-chunk', {
                        method: 'POST',
                        body: formData
                    });
                    
                    if (!response.ok) {
                        throw new Error(`Erreur chunk ${chunkIndex}`);
                    }
                    
                    const progress = ((chunkIndex + 1) / totalChunks) * 100;
                    queueItem.progress = progress;
                    
                    updateProgressBar(uploadId, progress);
                    updateUploadSpeed(uploadId, start + chunk.size, queueItem.startTime);
                    
                    if (chunkIndex === totalChunks - 1) {
                        queueItem.status = 'completed';
                        updateQueueItemStatus(uploadId, 'Terminé!', 'success');
                        activeUploads.delete(uploadId);
                        
                        setTimeout(() => {
                            removeQueueItemFromDOM(uploadId);
                            removeFromQueue(uploadId);
                        }, 3000);
                        
                        showNotification(`${file.name} téléversé avec succès`, 'success');
                        refreshFiles();
                    }
                }
            } catch (error) {
                queueItem.status = 'error';
                updateQueueItemStatus(uploadId, `Erreur: ${error.message}`, 'error');
                showNotification(`Erreur upload ${file.name}`, 'error');
            }
        }
        
        function updateProgressBar(uploadId, progress) {
            const progressBar = document.getElementById(`progress-${uploadId}`);
            if (progressBar) {
                progressBar.style.width = progress + '%';
                progressBar.textContent = Math.round(progress) + '%';
            }
        }
        
        function updateUploadSpeed(uploadId, bytesUploaded, startTime) {
            const elapsedSeconds = (Date.now() - startTime) / 1000;
            const speed = bytesUploaded / elapsedSeconds;
            const speedElement = document.getElementById(`speed-${uploadId}`);
            if (speedElement) {
                speedElement.textContent = `${formatSize(speed)}/s`;
            }
        }
        
        function updateQueueItemStatus(uploadId, message, type) {
            const statusElement = document.getElementById(`status-${uploadId}`);
            if (statusElement) {
                statusElement.textContent = message;
                statusElement.className = type;
            }
        }
        
        function pauseUpload(uploadId) {
            const queueItem = uploadQueue.find(item => item.id === uploadId);
            if (queueItem) {
                queueItem.status = 'paused';
                queueItem.pauseTime = Date.now();
                document.getElementById(`pause-${uploadId}`).classList.add('hidden');
                document.getElementById(`resume-${uploadId}`).classList.remove('hidden');
                updateQueueItemStatus(uploadId, 'En pause', 'warning');
            }
        }
        
        function resumeUpload(uploadId) {
            const queueItem = uploadQueue.find(item => item.id === uploadId);
            if (queueItem) {
                queueItem.status = 'queued';
                document.getElementById(`pause-${uploadId}`).classList.remove('hidden');
                document.getElementById(`resume-${uploadId}`).classList.add('hidden');
                updateQueueItemStatus(uploadId, 'Reprise...', 'info');
                processUploadQueue();
            }
        }
        
        function cancelUpload(uploadId) {
            const queueItem = uploadQueue.find(item => item.id === uploadId);
            if (queueItem) {
                queueItem.status = 'cancelled';
                activeUploads.delete(uploadId);
                removeQueueItemFromDOM(uploadId);
                removeFromQueue(uploadId);
                showNotification(`Upload annulé: ${queueItem.file.name}`, 'info');
            }
        }
        
        function pauseAllUploads() {
            uploadQueue.forEach(item => {
                if (item.status === 'active' || item.status === 'queued') {
                    pauseUpload(item.id);
                }
            });
            showNotification('Tous les uploads en pause', 'info');
        }
        
        function resumeAllUploads() {
            uploadQueue.forEach(item => {
                if (item.status === 'paused') {
                    resumeUpload(item.id);
                }
            });
            showNotification('Reprise de tous les uploads', 'success');
        }
        
        function cancelAllUploads() {
            if (confirm('Voulez-vous vraiment annuler tous les uploads?')) {
                uploadQueue.forEach(item => cancelUpload(item.id));
                showNotification('Tous les uploads annulés', 'info');
            }
        }
        
        function removeQueueItemFromDOM(uploadId) {
            const element = document.getElementById(`queue-${uploadId}`);
            if (element) {
                element.remove();
            }
        }
        
        function removeFromQueue(uploadId) {
            const index = uploadQueue.findIndex(item => item.id === uploadId);
            if (index > -1) {
                uploadQueue.splice(index, 1);
            }
        }
        
        function downloadFile(path, isFolder) {
            showNotification(`Téléchargement de ${path}...`, 'info');
            
            const url = isFolder ? `/api/download-folder/${encodeURIComponent(path)}` : `/api/download/${encodeURIComponent(path)}`;
            
            const link = document.createElement('a');
            link.href = url;
            link.download = path.split('/').pop();
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            
            // Ajouter à l'historique
            fetch('/api/add-history', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    action: 'download',
                    filename: path,
                    size: 0
                })
            });
        }
        
        function previewFile(path) {
            const extension = path.split('.').pop().toLowerCase();
            const imageExts = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp'];
            const textExts = ['txt', 'md', 'json', 'xml', 'csv'];
            
            if (imageExts.includes(extension)) {
                showImagePreview(path);
            } else if (textExts.includes(extension)) {
                showTextPreview(path);
            } else {
                showNotification('Aperçu non disponible pour ce type de fichier', 'info');
            }
        }
        
        function showImagePreview(path) {
            const modal = document.createElement('div');
            modal.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%; 
                background: rgba(0,0,0,0.8); z-index: 2000; display: flex; 
                align-items: center; justify-content: center;
            `;
            modal.onclick = () => modal.remove();
            
            const img = document.createElement('img');
            img.src = `/api/preview/${encodeURIComponent(path)}`;
            img.style.maxWidth = '90%';
            img.style.maxHeight = '90%';
            img.style.borderRadius = '10px';
            
            modal.appendChild(img);
            document.body.appendChild(modal);
        }
        
        function deleteFile(path) {
            if (confirm(`Voulez-vous vraiment supprimer "${path}"?`)) {
                fetch(`/api/delete/${encodeURIComponent(path)}`, { method: 'DELETE' })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            showNotification(`${path} supprimé`, 'success');
                            refreshFiles();
                        } else {
                            showNotification(`Erreur: ${data.error}`, 'error');
                        }
                    });
            }
        }
        
        function addToFavorites(path, name) {
            fetch('/api/favorites', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path, name })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showNotification(`${name} ajouté aux favoris`, 'success');
                } else {
                    showNotification(`Erreur: ${data.error}`, 'error');
                }
            });
        }
        
        function showFavorites() {
            fetch('/api/favorites')
                .then(response => response.json())
                .then(data => {
                    let html = '<h4>⭐ Favoris</h4>';
                    data.favorites.forEach(fav => {
                        html += `
                            <div style="padding: 10px; border: 1px solid #ddd; margin: 5px 0; border-radius: 5px;">
                                <strong>${fav.name}</strong><br>
                                <small>${fav.path} - ${fav.created_at}</small><br>
                                <button class="btn btn-primary" onclick="loadFiles('${fav.path}')">Ouvrir</button>
                                <button class="btn btn-danger" onclick="removeFavorite(${fav.id})">Suppr</button>
                            </div>
                        `;
                    });
                    showModal('Favoris', html);
                });
        }
        
        function showHistory() {
            fetch('/api/history')
                .then(response => response.json())
                .then(data => {
                    let html = '<h4>📊 Historique des activités</h4>';
                    data.history.forEach(item => {
                        const icon = item.action === 'upload' ? '📤' : '📥';
                        html += `
                            <div style="padding: 10px; border-bottom: 1px solid #eee;">
                                ${icon} <strong>${item.action}</strong>: ${item.filename}<br>
                                <small>Taille: ${formatSize(item.size)} - IP: ${item.ip_address} - ${item.timestamp}</small>
                            </div>
                        `;
                    });
                    showModal('Historique', html);
                });
        }
        
        function showModal(title, content) {
            const modal = document.createElement('div');
            modal.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%; 
                background: rgba(0,0,0,0.5); z-index: 2000; display: flex; 
                align-items: center; justify-content: center;
            `;
            
            const modalContent = document.createElement('div');
            modalContent.style.cssText = `
                background: white; padding: 30px; border-radius: 15px; 
                max-width: 80%; max-height: 80%; overflow-y: auto;
            `;
            modalContent.innerHTML = `
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                    <h3>${title}</h3>
                    <button onclick="this.closest('.modal').remove()" style="background: none; border: none; font-size: 1.5em; cursor: pointer;">×</button>
                </div>
                ${content}
            `;
            
            modal.className = 'modal';
            modal.appendChild(modalContent);
            document.body.appendChild(modal);
            
            modal.onclick = (e) => {
                if (e.target === modal) modal.remove();
            };
        }
        
        function showNotification(message, type = 'info') {
            const notification = {
                id: generateUUID(),
                message,
                type,
                timestamp: Date.now()
            };
            
            notifications.push(notification);
            
            const panel = document.getElementById('notification-panel');
            const container = document.getElementById('notifications');
            
            const notifDiv = document.createElement('div');
            notifDiv.className = `notification ${type}`;
            notifDiv.id = `notif-${notification.id}`;
            notifDiv.textContent = message;
            
            container.appendChild(notifDiv);
            panel.classList.remove('hidden');
            
            setTimeout(() => {
                notifDiv.remove();
                if (container.children.length === 0) {
                    panel.classList.add('hidden');
                }
            }, 5000);
        }
        
        function hideNotificationPanel() {
            document.getElementById('notification-panel').classList.add('hidden');
        }
        
        function cleanupOldNotifications() {
            const cutoff = Date.now() - 10000; // 10 secondes
            notifications = notifications.filter(n => n.timestamp > cutoff);
        }
        
        function generateUUID() {
            return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
                const r = Math.random() * 16 | 0;
                const v = c == 'x' ? r : (r & 0x3 | 0x8);
                return v.toString(16);
            });
        }
        
        function formatSize(bytes) {
            if (bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }
    </script>
</body>
</html>
    '''
    return html

# APIs étendues
@app.route('/api/stats')
def get_stats():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Compter les fichiers
    total_files = 0
    for root, dirs, files in os.walk(UPLOAD_FOLDER):
        total_files += len(files)
    
    # Uploads actifs
    cursor.execute("SELECT COUNT(*) FROM uploads WHERE status = 'active'")
    active_uploads = cursor.fetchone()[0]
    
    conn.close()
    
    return jsonify({
        'total_files': total_files,
        'active_uploads': active_uploads,
        'storage': get_storage_info()
    })

@app.route('/api/history')
def get_history():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM history ORDER BY timestamp DESC LIMIT 50')
    history = []
    for row in cursor.fetchall():
        history.append({
            'id': row[0],
            'action': row[1],
            'filename': row[2],
            'size': row[3],
            'ip_address': row[4],
            'timestamp': row[5]
        })
    
    conn.close()
    return jsonify({'history': history})

@app.route('/api/add-history', methods=['POST'])
def add_history_entry():
    data = request.json
    add_to_history(
        data.get('action'),
        data.get('filename'),
        data.get('size', 0),
        request.remote_addr
    )
    return jsonify({'success': True})

@app.route('/api/favorites', methods=['GET', 'POST'])
def handle_favorites():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    if request.method == 'POST':
        data = request.json
        try:
            cursor.execute('INSERT INTO favorites (path, name, created_at) VALUES (?, ?, ?)',
                         (data['path'], data['name'], datetime.now()))
            conn.commit()
            conn.close()
            return jsonify({'success': True})
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'success': False, 'error': str(e)})

@app.route('/api/upload-chunk', methods=['POST'])
def upload_chunk():
    """API pour upload par chunks avec reprise d'erreur"""
    try:
        chunk = request.files.get('chunk')
        file_name = request.form.get('fileName')
        chunk_index = int(request.form.get('chunkIndex'))
        total_chunks = int(request.form.get('totalChunks'))
        upload_id = request.form.get('uploadId')
        target_path = request.form.get('path', '')
        relative_path = request.form.get('relativePath', '')
        
        if not chunk or not file_name:
            return jsonify({'success': False, 'error': 'Chunk ou nom de fichier manquant'})
        
        # Utiliser le chemin relatif si disponible
        if relative_path:
            full_dir = os.path.join(UPLOAD_FOLDER, target_path, os.path.dirname(relative_path))
            final_path = os.path.join(UPLOAD_FOLDER, target_path, relative_path)
        else:
            filename = secure_filename(file_name)
            full_dir = os.path.join(UPLOAD_FOLDER, target_path)
            final_path = os.path.join(full_dir, filename)
        
        os.makedirs(full_dir, exist_ok=True)
        
        # Chemin temporaire pour les chunks
        temp_dir = os.path.join(TEMP_FOLDER, upload_id)
        os.makedirs(temp_dir, exist_ok=True)
        chunk_path = os.path.join(temp_dir, f"{file_name}.part{chunk_index}")
        
        # Sauvegarder le chunk
        chunk.save(chunk_path)
        
        # Mettre à jour le gestionnaire d'upload
        upload_manager.update_chunk(upload_id, chunk.content_length)
        
        # Si c'est le dernier chunk, assembler le fichier
        if chunk_index == total_chunks - 1:
            try:
                with open(final_path, 'wb') as final_file:
                    for i in range(total_chunks):
                        chunk_file_path = os.path.join(temp_dir, f"{file_name}.part{i}")
                        if os.path.exists(chunk_file_path):
                            with open(chunk_file_path, 'rb') as chunk_file:
                                final_file.write(chunk_file.read())
                            os.remove(chunk_file_path)
                        else:
                            raise Exception(f"Chunk {i} manquant")
                
                # Nettoyer le dossier temporaire
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass
                
                # Finaliser l'upload
                upload_manager.complete_upload(upload_id)
                
                # Ajouter à l'historique
                file_size = os.path.getsize(final_path)
                add_to_history('upload', file_name, file_size, request.remote_addr)
                
            except Exception as e:
                return jsonify({'success': False, 'error': f'Erreur assemblage: {str(e)}'})
        
        return jsonify({
            'success': True, 
            'chunk': chunk_index + 1, 
            'total': total_chunks,
            'upload_id': upload_id
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/upload', methods=['POST'])
def upload_files():
    """API pour upload de fichiers simples (fallback)"""
    try:
        files = request.files.getlist('files')
        relative_paths = request.form.getlist('relative_paths')
        target_path = request.form.get('path', '')
        
        if not files:
            return jsonify({'success': False, 'error': 'Aucun fichier sélectionné'})
        
        saved_files = []
        
        for i, file in enumerate(files):
            if file and file.filename:
                if i < len(relative_paths) and relative_paths[i]:
                    relative_path = relative_paths[i]
                    full_dir = os.path.join(UPLOAD_FOLDER, target_path, os.path.dirname(relative_path))
                    os.makedirs(full_dir, exist_ok=True)
                    filepath = os.path.join(UPLOAD_FOLDER, target_path, relative_path)
                else:
                    filename = secure_filename(file.filename)
                    full_dir = os.path.join(UPLOAD_FOLDER, target_path)
                    os.makedirs(full_dir, exist_ok=True)
                    filepath = os.path.join(full_dir, filename)
                
                file.save(filepath)
                saved_files.append(filepath)
                
                # Ajouter à l'historique
                file_size = os.path.getsize(filepath)
                add_to_history('upload', file.filename, file_size, request.remote_addr)
        
        return jsonify({
            'success': True,
            'files_saved': len(saved_files),
            'message': f'{len(saved_files)} fichier(s) uploadé(s) avec succès'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/files')
def get_files():
    """API pour obtenir la liste des fichiers avec tri"""
    path = request.args.get('path', '')
    sort_by = request.args.get('sort_by', 'name')
    sort_order = request.args.get('sort_order', 'asc')
    
    files = get_file_list(path, sort_by, sort_order)
    storage = get_storage_info()
    
    return jsonify({
        'files': files,
        'storage': storage,
        'path': path
    })

@app.route('/api/download/<path:filename>')
def download_file(filename):
    """API pour télécharger un fichier avec suivi"""
    try:
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(file_path):
            return jsonify({'error': 'Fichier non trouvé'}), 404
        
        # Ajouter à l'historique
        file_size = os.path.getsize(file_path)
        add_to_history('download', filename, file_size, request.remote_addr)
        
        return send_file(file_path, as_attachment=True)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download-folder/<path:foldername>')
def download_folder(foldername):
    """API pour télécharger un dossier en ZIP optimisé"""
    try:
        folder_path = os.path.join(UPLOAD_FOLDER, foldername)
        if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
            return jsonify({'error': 'Dossier non trouvé'}), 404
        
        # Créer un ZIP temporaire avec un nom unique
        zip_filename = f"{foldername.replace('/', '_')}_{int(time.time())}.zip"
        zip_path = os.path.join(TEMP_FOLDER, zip_filename)
        
        def generate_zip():
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as zipf:
                for root, dirs, files in os.walk(folder_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, folder_path)
                        zipf.write(file_path, arcname)
            
            return zip_path
        
        # Générer le ZIP dans un thread pour éviter le blocage
        zip_thread = threading.Thread(target=generate_zip)
        zip_thread.start()
        zip_thread.join(timeout=300)  # 5 minutes max
        
        if not os.path.exists(zip_path):
            return jsonify({'error': 'Erreur création ZIP'}), 500
        
        # Ajouter à l'historique
        folder_size = get_directory_size(folder_path)
        add_to_history('download', foldername, folder_size, request.remote_addr)
        
        def remove_file():
            time.sleep(10)  # Attendre que le téléchargement se termine
            try:
                os.remove(zip_path)
            except:
                pass
        
        # Supprimer le fichier ZIP après envoi
        threading.Thread(target=remove_file).start()
        
        return send_file(zip_path, as_attachment=True, 
                        download_name=f"{os.path.basename(foldername)}.zip")
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload-status/<upload_id>')
def get_upload_status(upload_id):
    """API pour obtenir le statut d'un upload"""
    status = upload_manager.get_upload_status(upload_id)
    if status:
        return jsonify(status)
    return jsonify({'error': 'Upload non trouvé'}), 404

@app.route('/api/cleanup-temp', methods=['POST'])
def cleanup_temp_files():
    """API pour nettoyer les fichiers temporaires"""
    try:
        # Supprimer les fichiers temporaires anciens (> 1 heure)
        current_time = time.time()
        cleanup_count = 0
        
        for root, dirs, files in os.walk(TEMP_FOLDER):
            for file in files:
                file_path = os.path.join(root, file)
                if current_time - os.path.getmtime(file_path) > RESUME_TIMEOUT:
                    try:
                        os.remove(file_path)
                        cleanup_count += 1
                    except:
                        pass
        
        # Nettoyer les dossiers vides
        for root, dirs, files in os.walk(TEMP_FOLDER, topdown=False):
            for dir in dirs:
                dir_path = os.path.join(root, dir)
                try:
                    os.rmdir(dir_path)
                except:
                    pass
        
        return jsonify({'success': True, 'cleaned_files': cleanup_count})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/server-info')
def server_info():
    """API pour informations détaillées du serveur"""
    return jsonify({
        'version': '2.0.0',
        'features': [
            'Upload par chunks',
            'Reprise d\'upload',
            'Uploads multiples',
            'Historique complet',
            'Favoris',
            'Aperçu fichiers',
            'Suppression sécurisée',
            'Interface responsive'
        ],
        'limits': {
            'max_concurrent_uploads': MAX_CONCURRENT_UPLOADS,
            'chunk_size': CHUNK_SIZE,
            'resume_timeout': RESUME_TIMEOUT
        },
        'stats': {
            'uptime': time.time(),
            'temp_folder_size': get_directory_size(TEMP_FOLDER)
        }
    })

def cleanup_old_uploads():
    """Fonction de nettoyage automatique des anciens uploads"""
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            # Supprimer les uploads anciens (> 24h)
            cutoff_time = datetime.now() - timedelta(hours=24)
            cursor.execute('DELETE FROM uploads WHERE status IN ("completed", "error") AND updated_at < ?', 
                         (cutoff_time,))
            
            conn.commit()
            conn.close()
            
            # Nettoyer les fichiers temporaires
            requests.post('http://localhost:5000/api/cleanup-temp')
            
        except:
            pass
        
        time.sleep(3600)  # Toutes les heures

if __name__ == '__main__':
    local_ip = get_local_ip()
    print(f"\n{'='*60}")
    print(f"🚀 SERVEUR DE PARTAGE WiFi PRO v2.0")
    print(f"{'='*60}")
    print(f"📍 Adresse IP locale: {local_ip}")
    print(f"🌐 URL d'accès: http://{local_ip}:5000")
    print(f"📁 Dossier de partage: {os.path.abspath(UPLOAD_FOLDER)}")
    print(f"🔧 Dossier temporaire: {os.path.abspath(TEMP_FOLDER)}")
    print(f"💾 Base de données: {os.path.abspath(DB_FILE)}")
    print(f"📦 Taille des chunks: {format_size(CHUNK_SIZE)}")
    print(f"⚡ Uploads simultanés max: {MAX_CONCURRENT_UPLOADS}")
    print(f"{'='*60}")
    print(f"🎯 NOUVELLES FONCTIONNALITÉS:")
    print(f"   ✅ Upload par chunks ultra-stable")
    print(f"   ✅ Queue d'upload avec pause/reprise")
    print(f"   ✅ Historique complet des activités")
    print(f"   ✅ Système de favoris")
    print(f"   ✅ Aperçu des fichiers (images/texte)")
    print(f"   ✅ Suppression sécurisée")
    print(f"   ✅ Recherche et tri avancés")
    print(f"   ✅ Interface ultra-moderne")
    print(f"   ✅ Notifications temps réel")
    print(f"   ✅ Statistiques détaillées")
    print(f"   ✅ Raccourcis clavier")
    print(f"   ✅ Mode responsive mobile")
    print(f"{'='*60}")
    print(f"⌨️  RACCOURCIS CLAVIER:")
    print(f"   Ctrl+U : Upload fichiers")
    print(f"   Ctrl+F : Recherche")
    print(f"   Ctrl+R : Actualiser")
    print(f"   ESC    : Fermer notifications")
    print(f"{'='*60}")
    print(f"💡 Instructions:")
    print(f"   1. Connectez les appareils au même réseau WiFi")
    print(f"   2. Ouvrez http://{local_ip}:5000 sur autres appareils")
    print(f"   3. Profitez de toutes les nouvelles fonctionnalités!")
    print(f"{'='*60}\n")
    
    # Démarrer le thread de nettoyage
    cleanup_thread = threading.Thread(target=cleanup_old_uploads, daemon=True)
    cleanup_thread.start()
    
    # Démarrer le serveur avec optimisations maximales
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.protocol_version = "HTTP/1.1"
    
    import werkzeug.serving
    werkzeug.serving.WSGIRequestHandler.timeout = 600  # 10 minutes timeout
    
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, 
            request_handler=WSGIRequestHandler) 'error': 'Déjà dans les favoris'})
    else:
        cursor.execute('SELECT * FROM favorites ORDER BY created_at DESC')
        favorites = []
        for row in cursor.fetchall():
            favorites.append({
                'id': row[0],
                'path': row[1],
                'name': row[2],
                'created_at': row[3]
            })
        conn.close()
        return jsonify({'favorites': favorites})

@app.route('/api/preview/<path:filename>')
def preview_file(filename):
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        return send_file(file_path)
    return jsonify({'error': 'Fichier non trouvé'}), 404

import os
import shutil
from flask import Flask, request, jsonify

UPLOAD_FOLDER = 'uploads'  # à adapter selon ton projet
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)

def add_to_history(action, filename, size, ip):
    # Fonction de log simplifiée
    print(f"[{action}] {filename} ({size} bytes) from {ip}")

@app.route('/api/delete/<path:filename>', methods=['DELETE'])
def delete_file(filename):
    try:
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(file_path):
            if os.path.isdir(file_path):
                shutil.rmtree(file_path)
                size = sum(os.path.getsize(os.path.join(dp, f)) for dp, dn, filenames in os.walk(file_path) for f in filenames)
            else:
                size = os.path.getsize(file_path)
                os.remove(file_path)
            
            add_to_history('delete', filename, size, request.remote_addr)
            return jsonify({'success': True, 'message': f"'{filename}' supprimé avec succès"})
        else:
            return jsonify({'success': False, 'error': 'Fichier ou dossier non trouvé'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)

