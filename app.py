import os
import uuid
import subprocess
import shutil
import hcl2
import logging
import threading
import time
from datetime import datetime
import werkzeug
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify, session

# Read environment variables, with default values if not set
FLASK_DEBUG = os.environ.get('FLASK_DEBUG', '0') == '1'  # Converts '0' to False, '1' to True
CLEANUP_INTERVAL = int(os.environ.get('CLEANUP_INTERVAL', 30))  # seconds
INACTIVITY_TIMEOUT = int(os.environ.get('INACTIVITY_TIMEOUT', 600))  # seconds

app = Flask(__name__, template_folder='templates')
app.debug = FLASK_DEBUG
app.secret_key = os.urandom(24)  # Secure random secret key
app.config['UPLOAD_FOLDER'] = os.path.abspath('sessions')
# app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB
# app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=20)

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Session management
sessions = {}
session_lock = threading.Lock()

@app.errorhandler(404)
def page_not_found(e):
    return "Page not found", 404

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.errorhandler(Exception)
def handle_error(e):
    if isinstance(e, werkzeug.exceptions.HTTPException):
        return e
    logger.exception("An error occurred")
    return jsonify(error=str(e)), 500

def cleanup_task():
    """Session cleanup daemon that removes both session records and files"""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = datetime.now()
        with session_lock:
            to_delete = []
            for session_id, last_active in list(sessions.items()):
                print(session_id," : ",last_active)
                if (now - last_active).total_seconds() > INACTIVITY_TIMEOUT:
                    to_delete.append(session_id)
            for session_id in to_delete:
                session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
                safe_delete(session_dir)
                if sessions.pop(session_id, None) is not None:
                    logger.info(f"Cleaned up inactive session: {session_id}")

cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
cleanup_thread.start()

def sanitize_path(path):
    """Prevent directory traversal attacks"""
    path = os.path.normpath(path).replace('\\', '/').lstrip('/')
    parts = []
    for part in path.split('/'):
        if part in ('.', ''):
            continue
        if part == '..':
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return '/'.join(parts)

def grant_full_access(path):
    """Windows permission fix"""
    if os.name == 'nt' and os.path.exists(path):
        try:
            subprocess.run(
                f'icacls "{path}" /grant *S-1-1-0:(OI)(CI)F /T',
                shell=True,
                check=True
            )
        except subprocess.CalledProcessError as e:
            logger.warning(f"Permission modification failed: {str(e)}")

def safe_delete(path, retries=5, delay=1):
    """Robust recursive deletion with retries"""
    for i in range(retries):
        try:
            grant_full_access(path)
            if os.path.exists(path):
                shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                return
            time.sleep(delay)
        except Exception as e:
            logger.warning(f"Delete attempt {i+1} failed: {str(e)}")
            time.sleep(delay * (i + 1))
    # Final force delete
    if os.path.exists(path):
        try:
            if os.name == 'nt':
                subprocess.run(f'rmdir /s /q "{path}"', shell=True, check=True)
            else:
                subprocess.run(['rm', '-rf', path], check=True)
        except Exception as e:
            logger.error(f"Force delete failed for {path}: {str(e)}")

def update_session_activity(session_id):
    with session_lock:
        sessions[session_id] = datetime.now()

@app.route('/', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        session_id = str(uuid.uuid4())
        session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
        os.makedirs(session_dir, exist_ok=True)

        files = request.files.getlist('files')
        if not files:
            return "No files selected", 400

        for file in files:
            if not file or file.filename == '':
                continue
            filename = sanitize_path(file.filename).replace('\\', '/')
            if not filename:
                continue

            if '/' in filename:
                target_path = os.path.join(session_dir, filename.split('/', 1)[1])
            else:
                target_path = os.path.join(session_dir, filename)

            target_abspath = os.path.abspath(target_path)
            upload_abspath = os.path.abspath(session_dir)
            if not target_abspath.startswith(upload_abspath):
                return f"Invalid file path: {filename}", 400

            os.makedirs(os.path.dirname(target_abspath), exist_ok=True)
            file.save(target_abspath)

        session['session_id'] = session_id
        update_session_activity(session_id)
        return redirect(url_for('variables', session_id=session_id))
    return render_template('upload.html')

@app.route('/variables/<session_id>', methods=['GET'])
def variables(session_id):
    update_session_activity(session_id)
    session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    variables = []
    
    variables_tf_path = os.path.join(session_dir, 'variables.tf')
    variables_tfvars_path = os.path.join(session_dir, 'terraform.tfvars')

    if os.path.exists(variables_tfvars_path):
        try:
            with open(variables_tfvars_path, 'r') as f:
                variables_tfvars = hcl2.load(f)
                print(variables_tfvars)
        except Exception as e:
            logger.error(f"Error parsing variables tfvars value: {str(e)}")


    if os.path.exists(variables_tf_path):
        try:
            with open(variables_tf_path, 'r') as f:
                variables_tf = hcl2.load(f)
                print(variables_tf)
                for var in variables_tf.get('variable', []):
                    var_name = list(var.keys())[0]
                    var_details = var[var_name]
                    if os.path.exists(variables_tfvars_path):
                        variables.append({
                        'name': var_name,
                        'type': var_details.get('type', 'string'),
                        'default': variables_tfvars.get(var_name, ''),
                        'description': var_details.get('description', '')
                    })
                    else:
                        variables.append({
                            'name': var_name,
                            'type': var_details.get('type', 'string'),
                            'default': var_details.get('default', ''),
                            'description': var_details.get('description', '')
                        })
        except Exception as e:
            logger.error(f"Error parsing variables: {str(e)}")
    
    return render_template('variables.html', session_id=session_id, variables=variables)

@app.route('/submit_variables/<session_id>', methods=['POST'])
def submit_variables(session_id):
    update_session_activity(session_id)
    session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    
    tf_vars = []
    variables_tf_path = os.path.join(session_dir, 'variables.tf')
    if os.path.exists(variables_tf_path):
        with open(variables_tf_path, 'r') as f:
            variables_tf = hcl2.load(f)
            tf_vars = [list(var.keys())[0] for var in variables_tf.get('variable', [])]
    
    with open(os.path.join(session_dir, 'terraform.tfvars'), 'w') as f:
        for var in tf_vars:
            value = request.form.get(var, '')
            f.write(f'{var} = "{value}"\n')

    return redirect(url_for('console', session_id=session_id))

@app.route('/download-tfvars/<session_id>')
def download_tfvars(session_id):
    session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    tfvars_path = os.path.join(session_dir, 'terraform.tfvars')

    if not os.path.exists(tfvars_path):
        return jsonify(error="File not found"), 404

    with open(tfvars_path, 'rb') as f:
        content = f.read()

    response = Response(
        content,
        mimetype='text/plain',
        headers={
            'Content-Disposition': 'attachment; filename="terraform.tfvars"',
            'Cache-Control': 'no-store'
        }
    )
    return response

@app.route('/console/<session_id>')
def console(session_id):
    update_session_activity(session_id)
    return render_template('console.html', session_id=session_id)

@app.route('/run-command/<session_id>', methods=['POST'])
def run_command(session_id):
    update_session_activity(session_id)
    data = request.get_json(silent=True) or {}
    command = data.get('command')
    session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)

    commands = {
        'init': ['terraform', 'init'],
        'plan': ['terraform', 'plan', '-var-file=terraform.tfvars'],
        'apply': ['terraform', 'apply', '-auto-approve', '-var-file=terraform.tfvars'],
        'plan-destroy': ['terraform', 'plan', '-destroy', '-var-file=terraform.tfvars'],
        'destroy': ['terraform', 'destroy', '-auto-approve', '-var-file=terraform.tfvars']
    }

    if command not in commands:
        return {'error': 'Invalid command'}, 400

    def generate():
        needs_init = not os.path.exists(os.path.join(session_dir, '.terraform'))

        if command != 'init' and needs_init:
            init_process = subprocess.Popen(
                commands['init'],
                cwd=session_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True
            )

            yield "\n⚠️ Running automatic init first...\n\n"
            for line in iter(init_process.stdout.readline, ''):
                yield line
            init_process.stdout.close()
            return_code = init_process.wait()

            if return_code != 0:
                yield "\n❌ Init failed - cannot proceed\n"
                return

        process = subprocess.Popen(
            commands[command],
            cwd=session_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            env={**os.environ, 'TERM': 'xterm-256color'}
        )

        if command != 'init' and needs_init:
            yield "\n➡️ Now running command...\n\n"

        for line in iter(process.stdout.readline, ''):
            yield line
        process.stdout.close()
        return_code = process.wait()

        yield f"\nProcess completed with exit code {return_code}\n"

    return Response(generate(), mimetype='text/plain')

@app.route('/cleanup/<session_id>', methods=['POST'])
def explicit_cleanup(session_id):
    session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    try:
        safe_delete(session_dir)
        logger.info(f"Successfully cleaned up session: {session_id}")
    except Exception as e:
        logger.error(f"Failed to clean session {session_id}: {str(e)}")

    with session_lock:
        sessions.pop(session_id, None)

    return '', 204

@app.route('/heartbeat/<session_id>', methods=['POST'])
def heartbeat(session_id):
    update_session_activity(session_id)
    return '', 204

if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=True)
