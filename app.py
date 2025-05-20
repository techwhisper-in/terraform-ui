import os
import uuid
import subprocess
import shutil
import hcl2
import logging
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.abspath('sessions')  # Use absolute path
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB


# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Session management
sessions = {}
session_lock = threading.Lock()
CLEANUP_INTERVAL = 300  # 5 minutes
INACTIVITY_TIMEOUT = 1800  # 30 minutes
global root_dir
def cleanup_task():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = datetime.now()
        with session_lock:
            to_delete = []
            for session_id, last_active in sessions.items():
                if (now - last_active).total_seconds() > INACTIVITY_TIMEOUT:
                    to_delete.append(session_id)
            
            for session_id in to_delete:
                session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
                if os.path.exists(session_dir):
                    try:
                        shutil.rmtree(session_dir)
                        del sessions[session_id]
                        logger.info(f"Cleaned up inactive session: {session_id}")
                    except Exception as e:
                        logger.error(f"Error cleaning session {session_id}: {str(e)}")

cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
cleanup_thread.start()

def sanitize_path(path):
    """Sanitize file path to prevent directory traversal"""
    # Normalize path and convert to POSIX format
    path = os.path.normpath(path).replace('\\', '/')
    
    # Remove leading slash and split into components
    path = path.lstrip('/')
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
    """Grant full access to path (Windows only)"""
    if os.name == 'nt':
        try:
            subprocess.run(
                f'icacls "{path}" /grant *S-1-1-0:(OI)(CI)F /T',
                shell=True,
                check=True
            )
        except subprocess.CalledProcessError as e:
            logger.warning(f"Permission modification failed: {str(e)}")

def safe_delete(path, retries=3, delay=1):
    for i in range(retries):
        try:
            grant_full_access(path)
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                return
            time.sleep(delay)
        except Exception as e:
            logger.warning(f"Delete attempt {i+1} failed: {str(e)}")
            time.sleep(delay)
    
    if os.name == 'nt' and os.path.exists(path):
        try:
            subprocess.run(f'rmdir /s /q "{path}"', shell=True, check=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Final force delete failed: {str(e)}")


def update_session_activity(session_id):
    with session_lock:
        sessions[session_id] = datetime.now()

@app.errorhandler(Exception)
def handle_error(e):
    logger.exception("An error occurred")
    return jsonify(error=str(e)), 500

@app.route('/', methods=['GET', 'POST'])
def upload(): 
    session_id = str(uuid.uuid4())
    session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    os.makedirs(session_dir, exist_ok=True)

    if request.method == 'POST':
        files = request.files.getlist('files')
        if not files:
            return "No files selected", 400
        for file in files:
            if file.filename == '':
                continue

            # Sanitize filename
            filename = sanitize_path(file.filename)
            if not filename:
                continue  # Skip invalid paths
            
            # Create full target path
            #print("Filename:  ",filename)
            target_path = os.path.join(session_dir, filename.split('/', 1)[1])
            
            # Convert to absolute paths for validation
            target_abspath = os.path.abspath(target_path)
            upload_abspath = os.path.abspath(session_dir)

            # Verify the target is within the upload directory
            if not target_abspath.startswith(upload_abspath):
                return f"Invalid file path: {filename}", 400

            # Create directories if needed
            os.makedirs(os.path.dirname(target_abspath), exist_ok=True)
            file.save(target_abspath)

        #return f"Files uploaded successfully to session: {session_id}"
        return redirect(url_for('variables', session_id=session_id))


    return render_template('upload.html')

@app.route('/variables/<session_id>', methods=['GET'])
def variables(session_id):
    update_session_activity(session_id)
    session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    variables = []
    variables_tf_path = os.path.join(session_dir, 'variables.tf')
    #print(variables_tf_path)
    if os.path.exists(variables_tf_path):
        try:
            with open(variables_tf_path, 'r') as f:
                variables_tf = hcl2.load(f)
                #print(variables_tf)
                for var in variables_tf.get('variable', []):
                    var_name = list(var.keys())[0]
                    var_details = var[var_name]
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

@app.route('/console/<session_id>')
def console(session_id):
    update_session_activity(session_id)
    return render_template('console.html', session_id=session_id)

@app.route('/run-command/<session_id>', methods=['POST'])
def run_command(session_id):
    update_session_activity(session_id)
    data = request.get_json()
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
        
        # Automatically run init if needed for plan/apply/destroy
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
        
        # Run requested command
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
        
        if command == 'destroy' and return_code == 0:
            shutil.rmtree(session_dir, ignore_errors=True)
            with session_lock:
                if session_id in sessions:
                    del sessions[session_id]
        
        yield f"\nProcess completed with exit code {return_code}\n"
    
    return Response(generate(), mimetype='text/plain')

@app.route('/cleanup/<session_id>', methods=['POST'])
def explicit_cleanup(session_id):
    session_dir = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    
    if os.path.exists(session_dir):
        try:
            safe_delete(session_dir)
        except Exception as e:
            logger.error(f"Error cleaning {session_id}: {str(e)}")
    
    with session_lock:
        if session_id in sessions:
            del sessions[session_id]
    
    return '', 204
@app.route('/heartbeat/<session_id>', methods=['POST'])
def heartbeat(session_id):
    update_session_activity(session_id)
    return '', 204

if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=True)
