import configparser
import json
import os
import time
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

global local_connection

consumersConfigs = {
    "exchangeName": "TEST_STATUSES",
    "queueName": "TEST_STATUSES"
}


def clean_after_test(ch, method, properties, body):
    print("----")
    user = config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_USERNAME', 'guest')
    password = config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_PASSWORD', 'guest')
    host = config['DEFAULT'].get('LOCAL_RABBITMQ_SERVER_ADDRESS', 'localhost')
    port = config['DEFAULT'].get('LOCAL_RABBITMQ_ADMIN_PORT', '15672')

    test_id = properties.headers['suiteId']
    while True:
        # check if there are any messages in the queue
        try:
            queue_messages = requests.post(
                url=f"http://{user}:{password}@{host}:{port}/api/queues/%2f/test-{test_id}/get",
                json={
                    "count": "1",
                    "requeue": "false",
                    "encoding": "auto",
                    "truncate": 5000,
                    "ackmode": "ack_requeue_false"
                }
            )
            if queue_messages.text == '[]':
                # delete queue and shovel
                requests.delete(url=f"http://{user}:{password}@{host}:{port}/api/parameters/shovel/%2f/relay-{test_id}")
                requests.delete(url=f"http://{user}:{password}@{host}:{port}/api/queues/%2f/test-{test_id}")
                print(f'Deleted shovel and queue for test {test_id}')
                return
            elif queue_messages.status_code == 404:
                return
        except (Exception,):
            pass
        time.sleep(1)


def connect_to_local():
    global local_connection
    global local_channel
    local_connection = pika.BlockingConnection(local_connection_params)
    local_channel = local_connection.channel()
    print(f'Opening connection with {consumersConfigs}')
    local_channel.queue_bind(
        queue=consumersConfigs["queueName"],
        exchange=consumersConfigs["exchangeName"]
    )


local_channel = None


@retry(pika.exceptions.AMQPConnectionError, delay=5, jitter=(1, 3))
def await_suites_to_be_finished():
    global local_connection
    global local_channel
    connect_to_local()
    print("Listening.")
    local_channel.basic_qos(prefetch_count=0)
    local_channel.basic_consume(
        queue=consumersConfigs["queueName"],
        consumer_tag='Cleanup manager',
        on_message_callback=clean_after_test,
        auto_ack=False
    )

    try:
        local_channel.start_consuming()
    except KeyboardInterrupt:
        print("Keyboard interrupt. Stopping gently")
        local_channel.stop_consuming()
        local_connection.channel().queue_delete(queue=consumersConfigs["queueName"])
        local_connection.close()
    # Do not recover on channel errors
    except pika.exceptions.ConnectionClosedByBroker as err:
        print("Caught a channel error: {}, stopping...".format(err))


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
    client_properties={'connection_name': 'Cleanup manager'},
    heartbeat=5
)

# Set up the runner exchange if it doesn't exist
local_connection = pika.BlockingConnection(local_connection_params)
local_connection.channel().exchange_declare(exchange=consumersConfigs["exchangeName"], exchange_type='fanout', durable=True)
local_connection.channel().queue_declare(queue=consumersConfigs["queueName"], durable=True)
local_connection.channel().close()
local_connection.close()

await_suites_to_be_finished()
