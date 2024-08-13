import configparser
import json
import shutil
import threading
import time
import uuid
import os
import subprocess
import platform


class Starter(threading.Thread):
    output_absolute_path = None
    suite_id = None
    config = configparser.ConfigParser()
    CWD = os.path.dirname(os.path.realpath(__file__))
    os.chdir(CWD)
    config.read('../config.env')

    def __init__(self, rabbit_container):
        threading.Thread.__init__(self)
        self.rabbit_container = rabbit_container

    def configure_absolute_path(self, file_name):
        self.output_absolute_path = os.path.abspath(file_name)

    def save_file(self, payload):
        self.suite_id = payload['suiteId']
        test_id = uuid.uuid4()
        file_name = f"{test_id}.json"
        with open(file_name, "w") as output_file:
            json.dump(payload, output_file)
        self.configure_absolute_path(file_name)
        print(f'Saving {file_name}')

    def run(self) -> None:
        runner_path = self.config['DEFAULT']['RUNNER_PATH']
        if self.config['DEFAULT'].get('MANUAL_MODE', 'False') in ['True', 'true', '1', True]:
            shutil.copyfile(self.output_absolute_path, f'{runner_path}/output.json')
            return

        if platform.system() == 'Windows':
            venv_install_command = ['pip', 'install', 'virtualenv']
            venv_command = ['virtualenv', f'{runner_path}\\venv']
            req_command = [f'{runner_path}\\venv\\Scripts\\pip.exe', 'install', '-r', f'{runner_path}\\requirements.txt']
            run_command = [f'{runner_path}\\venv\\Scripts\\python.exe', f'{runner_path}\\CodeGenerator.py', self.output_absolute_path]
        else:
            venv_install_command = None
            venv_command = ['python', '-m', 'venv', f'{runner_path}/venv']
            req_command = [f'{runner_path}/venv/bin/pip', 'install', '-r', f'{runner_path}/requirements.txt']
            run_command = [f'{runner_path}/venv/bin/python', f'{runner_path}/CodeGenerator.py', self.output_absolute_path]

        if venv_install_command:
            subprocess.run(venv_install_command, text=True, capture_output=True)
        subprocess.run(venv_command, text=True, capture_output=True)
        subprocess.run(req_command, text=True, capture_output=True)
        container_output = subprocess.run(run_command, text=True, capture_output=True)
        print(container_output)
        try:
            self.delete_shovel()
        except (Exception,):
            pass

    def delete_shovel(self):
        while True:
            # check if there are any messages in the queue
            queue_messages = self.rabbit_container.exec_run(f"rabbitmqadmin get queue=suite-{self.suite_id}")\
                                .output.decode('utf-8').lower()
            if 'no items' in queue_messages:
                self.rabbit_container.exec_run(f'rabbitmqctl clear_parameter shovel relay-{self.suite_id}')
                self.rabbit_container.exec_run(f'rabbitmqctl delete_queue suite-{self.suite_id}')
                print(f'Deleted shovel and queue for suite {self.suite_id}')
                return
            elif 'not found' in queue_messages:
                return
            time.sleep(1)
