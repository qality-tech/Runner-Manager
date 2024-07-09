# RunnerManager
The purpose of this project is to listen to a rabbitmq queue and start a new runner for each message intended for it.

# Moto:
The test logs should be stored locally and then re-forwarded as many times as needed. They are critical for the test's lifetime.

- The runners should be split on technology. For example for Web, one for Mobile, one for API, etc.
- Multiple runners of the same technology can be started simultaneously. Where applicable and makes sense they can be started in docker containers.

A container with a local rabbitmq instance is started locally to relay the reports' messages forward to the main rabbitmq instance.
The reports can significantly vary in size, complexity and intensity thus an API approach is not suitable.
For each test running in a runner a new temporary queue is created and the shovel plugin is used to relay the messages to the main rabbitmq instance.

# Installation
- Install docker 23.0.3+
- Install Python 3.10+
- Install pipenv 2024.0.1 or any other virtual environment manager
- Run `python pipenv install` to install the dependencies or the equivalent command for your virtual environment manager
- Copy `config.env.example` to `config.env` and fill in the necessary values
- Run `python RunnerManager.py` to start the runner manager
- Run `python CleanupManager.py` to start the cleanup manager
- Enjoy
