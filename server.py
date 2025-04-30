from flask import Flask, request, jsonify
import os
from datetime import datetime

app = Flask(__name__)
UPLOAD_FOLDER = "uploaded_files"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route("/upload", methods=["POST"])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    ip = request.remote_addr
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_dir = os.path.join(UPLOAD_FOLDER, ip.replace('.', '_'))
    os.makedirs(save_dir, exist_ok=True)
    filepath = os.path.join(save_dir, f"{timestamp}_{file.filename}")
    file.save(filepath)
    return jsonify({"status": "success", "filename": filepath}), 200

@app.route("/")
def index():
    return "Serveur de réception de fichiers opérationnel."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
