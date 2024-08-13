import configparser
import json
import logging
import os
from contextlib import suppress
from time import sleep

import docker
import pika
import requests
from retry import retry

config = configparser.ConfigParser()
CWD = os.path.dirname(os.path.realpath(__file__))
os.chdir(CWD)
# --

if not os.path.exists('config.env'):
    print('config.env file does not exist. Please create one')
    exit(1)

config.read('config.env')
rabbit_local_relay = None
tests_tested = {}
shovels_created = []

try:
    docker = docker.from_env()
except:
    pass

target_credentials = pika.credentials.PlainCredentials(
    username=config['DEFAULT']['TARGET_RABBITMQ_SERVER_USERNAME'],
    password=config['DEFAULT']['TARGET_RABBITMQ_SERVER_PASSWORD']
)

target_connection_params = pika.ConnectionParameters(
    host=config['DEFAULT']['TARGET_RABBITMQ_SERVER_ADDRESS'],
    port=int(config['DEFAULT']['TARGET_RABBITMQ_SERVER_PORT']),
    credentials=target_credentials,
    heartbeat=None,
    client_properties={'connection_name': f'RunnerManager_{config["DEFAULT"].get("RUNNER_NAME", "QA_LOCAL")}'}
)

global local_connection

runnerConfiguration = {
    "cpu": "1",
    "gpu": "0",
    "network": "global",
    "name": config['DEFAULT'].get('RUNNER_NAME', 'QA_LOCAL'),
    "x-match": "all"
}

consumersConfigs = {
    "exchangeName": "AvailableWork",
    "queueName": "API",
    "routingKey": "API",
}


def publish_forward(ch, method, properties, body):
    create_shovel(ch, method, properties, body)
    if config['DEFAULT'].get('MANUAL_MODE', 'False').lower() in ['false', '0']:
        local_connection = pika.BlockingConnection(local_connection_params)
        local_connection.channel().basic_publish(
            exchange='',
            routing_key='RUNNER' if method.routing_key != 'IOS' else 'IOS',
            body=body)
        local_connection.close()
    else:
        save_test(body)
    ch.basic_ack(method.delivery_tag)


def spawn_container(ch, method, properties, body):
    print(f"---- {method.INDEX} : {method.routing_key} ----")

    if 'name' not in properties.headers or properties.headers['name'] is None:
        print("Rejected - the runner name was not present in the header.")
        ch.basic_reject(method.delivery_tag)
        return

    if properties.headers['name'].lower() != runnerConfiguration['name'].lower():
        print(f"Rejected - This test is for {properties.headers['name']}.")
        ch.basic_reject(method.delivery_tag)
        return

    # TODO: if there are no more cores available, reject the message
    # avoid testing the same suite multiple times
    decoded_body = json.loads(body.decode('utf-8'))

    if method.routing_key == 'IOS':
        print("Forwarded - IOS")
        publish_forward(ch, method, properties, body)
        return

    if 'suiteId' not in decoded_body.keys():
        print("Fake ack for cleanup - 'suiteId' was not present in the decoded_body.")
        ch.basic_ack(method.delivery_tag)
        return

    if decoded_body['suiteId'] in tests_tested.keys() and decoded_body['id'] in tests_tested[decoded_body['suiteId']]:
        print("Skipped - test already running.")
        ch.basic_ack(method.delivery_tag)
        return

    print(ch)
    print(method)
    print(properties)

    if decoded_body['suiteId'] not in tests_tested.keys():
        tests_tested[decoded_body['suiteId']] = []
    tests_tested[decoded_body['suiteId']].append(decoded_body['id'])

    publish_forward(ch, method, properties, body)
    print("----")


def save_test(payload):
    file_name = f"output.json"
    with open(file_name, "w") as output_file:
        json.dump(json.loads(payload.decode('utf-8')), output_file)
    print(f'Saving {file_name}')


def connect_to_target():
    global target_connection
    global target_channel
    target_connection = pika.BlockingConnection(target_connection_params)
    target_channel = target_connection.channel()
    print(f'Opening connection with {consumersConfigs}')
    print(f'-configuration {runnerConfiguration}')
    target_channel.queue_bind(
        queue=consumersConfigs["queueName"],
        exchange=consumersConfigs["exchangeName"],
        routing_key=consumersConfigs["routingKey"],
        arguments=runnerConfiguration)


def get_suite_id_from_body(body):
    decoded_body = body.decode("utf-8")
    try:
        id_start_position = decoded_body.find("suiteId")
        id_end_position = decoded_body.find(",", id_start_position)
        return int(decoded_body[id_start_position:id_end_position].split(":")[-1].strip())
    except:
        body_json = json.loads(decoded_body)
        return int(body_json["suiteId"])


def get_test_id_from_body(body):
    decoded_body = body.decode("utf-8")
    try:
        id_start_position = decoded_body.find("id")
        id_end_position = decoded_body.find(",", id_start_position)
        return int(decoded_body[id_start_position:id_end_position].split(":")[-1].strip())
    except:
        body_json = json.loads(decoded_body)
        return int(body_json["id"])


def create_shovel(ch, method, properties, body):
    suite_id = get_suite_id_from_body(body)
    test_id = get_test_id_from_body(body)
    if test_id in shovels_created:
        return
    shovels_created.append(test_id)
    print(f'Creating shovel relay-{test_id}')
    try:
        rabbit_local_relay.exec_run(f'rabbitmqadmin declare queue name=test-{test_id}')
        remote_uri = f'amqp://{config["DEFAULT"]["TARGET_RABBITMQ_SERVER_USERNAME"]}:' \
                     f'{config["DEFAULT"]["TARGET_RABBITMQ_SERVER_PASSWORD"]}@' \
                     f'{config["DEFAULT"]["TARGET_RABBITMQ_SERVER_ADDRESS"]}:' \
                     f'{config["DEFAULT"]["TARGET_RABBITMQ_SERVER_PORT"]}'
        params = {
            "value": {
                "src-protocol": "amqp091",
                "src-uri": f"amqp://{config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_USERNAME', 'guest')}:"
                           f"{config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_PASSWORD', 'guest')}@"
                           f"{config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_ADDRESS', 'localhost')}",
                "src-queue": f"test-{test_id}",
                "src-prefetch-count": 0,
                "dest-protocol": "amqp091",
                "dest-uri": remote_uri,
                "dest-exchange": "SUITES",
                "dest-exchange-key": f'suite-{suite_id}'
            }
        }
        requests.put(
            url=f'http://{config["DEFAULT"].get("LOCAL_RABBITMQ_SERVER_ADDRESS", "localhost")}:'
                f'{config["DEFAULT"].get("LOCAL_RABBITMQ_SERVER_ADMIN_PORT", "15672")}'
                f'/api/parameters/shovel/%2f/relay-{test_id}',
            auth=(
                config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_USERNAME', 'guest'),
                config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_PASSWORD', 'guest')),
            json=params
        )
        print('Created shovel')
    except Exception as e:
        print(f'Creation failed with {e.args}')


target_connection = None
target_channel = None


def start_rabbit_local_relay():
    logging.info("Stopping existing rabbit-local-relay container if it exists")
    with suppress(Exception):
        existing_container = docker.containers.get('rabbit-local-relay')
        existing_container.stop()
        existing_container.remove()

    print("Building rabbit-local-relay image")
    global rabbit_local_relay
    docker.images.build(path="./",
                        tag="rabbit-local-relay-image")

    print("Starting rabbit-local-relay container")
    rabbit_local_relay = docker.containers.run(
        docker.images.get('rabbit-local-relay-image'),
        environment={'RABBITMQ_DEFAULT_USER': config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_USERNAME', 'guest'),
                     'RABBITMQ_DEFAULT_PASS': config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_PASSWORD', 'guest')},
        ports={
            '15671': 15671,
            '15672': config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_ADMIN_PORT', '15672'),
            '15691': 15691,
            '15692': 15692,
            '25672': 25672,
            '4369': 4369,
            '5671': 5671,
            '5672': config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_PORT', '5672')
        },
        restart_policy={"Name": "on-failure", "MaximumRetryCount": 5},
        name="rabbit-local-relay",
        detach=True)

    print("Waiting for rabbit-local-relay to start")
    timeout = 120
    stop_time = 3
    elapsed_time = 0
    while rabbit_local_relay.status != 'running' and elapsed_time < timeout:
        sleep(stop_time)
        elapsed_time += stop_time
        rabbit_local_relay = docker.containers.get(rabbit_local_relay.id)

    print("rabbit-local-relay has started")


def stop_rabbit_local_relay():
    rabbit_local_relay.stop(timeout=1)
    print('Rabbit-local-relay stopped')
    rabbit_local_relay.remove()
    print('Rabbit-local-relay removed')


@retry(pika.exceptions.AMQPConnectionError, delay=5, jitter=(1, 3))
def await_suites_to_be_run():
    global target_connection
    global target_channel
    connect_to_target()
    print("Listening.")
    target_channel.basic_qos(prefetch_count=0)
    target_channel.basic_consume(
        queue=consumersConfigs["queueName"],
        consumer_tag=f'RunnerManager_{runnerConfiguration["name"]}',
        on_message_callback=spawn_container,
        auto_ack=False
    )

    try:
        target_channel.start_consuming()
    except KeyboardInterrupt:
        print("Keyboard interrupt. Stopping gently")
        target_channel.stop_consuming()
        target_connection.close()
        stop_rabbit_local_relay()
    # Do not recover on channel errors
    except pika.exceptions.ConnectionClosedByBroker as err:
        stop_rabbit_local_relay()
        print("Caught a channel error: {}, stopping...".format(err))


try:
    start_rabbit_local_relay()
except Exception as e:
    print(f'Could not create local rabbit instance. Error: {e}')

timeout = 0

while timeout < 120:
    try:
        host = config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_ADDRESS', 'localhost')
        port = config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_ADMIN_PORT', '15672')
        response = requests.get(
            url=f'http://{host}:{port}/api/nodes',
            auth=(
                config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_USERNAME', 'guest'),
                config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_PASSWORD', 'guest')
            )
        )
    except Exception as e:
        print(f'Could not connect to local rabbit instance. Error: {e}')
        sleep(1)
        timeout += 1
        continue

    if response.status_code == 200:
        try:
            if json.loads(response.text)[0]['running']:
                print('Local rabbit instance is running')
                break
        except Exception as e:
            print(f'Not getting good response from rabbit. Error: {e}')
            sleep(1)
            timeout += 1
            continue

local_credentials = pika.credentials.PlainCredentials(
    username=config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_USERNAME', 'guest'),
    password=config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_PASSWORD', 'guest')
)

local_connection_params = pika.ConnectionParameters(
    host=config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_ADDRESS', 'localhost'),
    port=int(config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_PORT', '5672')),
    credentials=local_credentials,
    client_properties={'connection_name': 'RunnerManager - local'},
    heartbeat=5
)

# Set up the runner exchange if it doesn't exist
local_connection = pika.BlockingConnection(local_connection_params)
local_connection.channel().queue_declare(queue='RUNNER', durable=True)
local_connection.channel().close()
local_connection.close()

await_suites_to_be_run()
