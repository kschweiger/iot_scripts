import logging
import signal
from datetime import datetime
from multiprocessing import Pipe, Process
from multiprocessing.connection import Connection
from random import random
from subprocess import PIPE, Popen
from time import sleep, time
from typing import Dict
import requests

# BME280 WEATHER SENSOR
from bme280 import BME280
from dynaconf import Dynaconf

# MICS6814 ANALOG GAS SENSOR
from enviroplus import gas

# LTR-559 LIGHT AND PROXIMITY SENSOR
from ltr559 import LTR559

logging.basicConfig(
    format="%(asctime)s %(funcName)-12s %(process)d %(levelname)-8s %(message)s",
    level=logging.DEBUG,
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("__name__")


# Initialize the sensors
bme280 = BME280()
ltr559 = LTR559()


def get_cpu_temperature():
    """From https://github.com/pimoroni/enviroplus-python/blob/master/examples/combined.py"""
    process = Popen(["vcgencmd", "measure_temp"], stdout=PIPE, universal_newlines=True)
    output, _error = process.communicate()
    return float(output[output.index("=") + 1 : output.rindex("'")])


def read_all_environment_data(temp_calibration_factor: float, cpu_temps: list[float]):
    cpu_temp = get_cpu_temperature()
    # Smooth out with some averaging to decrease jitter
    cpu_temps = cpu_temps[1:] + [cpu_temp]
    avg_cpu_temp = sum(cpu_temps) / float(len(cpu_temps))

    raw_temp = bme280.get_temperature()
    temp = raw_temp - ((avg_cpu_temp - raw_temp) / temp_calibration_factor)
    pres = bme280.get_pressure()
    hum = bme280.get_humidity()

    lux = ltr559.get_lux()

    gas_data = gas.read_all()
    gas_ox = gas_data.oxidising / 1000
    gas_red = gas_data.reducing / 1000
    gas_nh3 = gas_data.nh3 / 1000

    timestamp = datetime.now()

    return (timestamp, temp, pres, hum, lux, gas_ox, gas_red, gas_nh3)


def read_data(
    initial_read_time: int,
    read_resolution: int,
    send_batch_size: int,
    temp_calibration_factor: float,
    conn_data: Connection,
    conn_kill: Connection,
):
    """
    Function handling reading the sensors of the Enviro Module running with the Pi. Funtion
    will run until receiving anything via the kill_conn. Before exiting, will send a None
    via the data connection.

    :param initial_read_time: Time windows that will be used to query the sensors before
                              data will be collected and sent to the sending process
    :param read_resolution: Time resolution of the data measurements
    :param send_batch_size: Defined how many measurements will be send to the sending process
                            per batch
    :param temp_calibration_factor: Temperatur calibration factor. Following the enviro
                                    examples
    :param conn_data: Sending connection for the data
    :param conn_kill: Receiving connection waiting for the  kill command from the parent process
    """
    logger.info("Data reader started")

    cpu_temps = [get_cpu_temperature()] * 5

    logger.info(
        "Reading initial data pre saving - Duration: %s [sec]", initial_read_time
    )
    for i in range(initial_read_time // read_resolution):
        data = read_all_environment_data(temp_calibration_factor, cpu_temps)
        # logger.info(data)
        sleep(read_resolution)

    keep_reading = True

    data_batch = []
    package_sent = 0
    stop_reading_data = False
    while keep_reading:
        data = read_all_environment_data(temp_calibration_factor, cpu_temps)

        time_pre = time()
        data_batch.append(data)

        if len(data_batch) == send_batch_size:
            conn_data.send(data_batch)
            data_batch = []
            package_sent += 1

            if stop_reading_data:
                break

        exit_sig_received = conn_kill.poll()
        if exit_sig_received and not stop_reading_data:
            logger.warning("Received stop signal. Will stop after next batch was sent")
            stop_reading_data = True

        sleep_for = read_resolution - (time() - time_pre)
        logger.debug("Sleeping for %s seconds", sleep_for)
        sleep(sleep_for)

    conn_data.send(None)
    logger.info("Data reader exiting")


def send_data(url: str, header: Dict[str, str], conn_data: Connection):
    """
    Function handling sending the data to a server via a post requests.
    Waits for data from the data connection. If None is recveived, the function
    exits.

    :param url: Url for the post request to the server
    :param header: Header object for the post requests containing the API key
    :param conn_data: Receiving connection for the data
    """
    logger.info("Receiver: Running")
    while True:
        batch = conn_data.recv()
        if batch is None:
            break
        logger.debug(f"Got {batch}")
        # TODO: implement sending the data via post request

    logger.info("Receiver: Done")


def main():
    """
    Main processes started fomr the CLI. Handles setup, process creation and teardown.
    """
    settings = Dynaconf(
        settings_files=["settings.toml", ".secrets.toml"],
    )
    data_receiver_url = (
        f"http://{settings.receiver.host}:{settings.receiver.port}/environment"
    )
    header = {"access_token": settings.enviro_monitor.api_key}

    logger.info("===== Setup ======")
    logger.info(
        "Initial read timespan: %s [sec]", settings.enviro_monitor.init_read_time
    )
    logger.info(
        "Time resolution of the data: %s [sec]", settings.enviro_monitor.resolution
    )
    logger.info("Data batch size: %s", settings.enviro_monitor.batch_size_send)
    logger.info("Request URL: %s", data_receiver_url)

    # Somehow we need this single handler thing so we kan ctrl+c in the main loop
    # and not also send it to the processes started.
    # Source: https://stackoverflow.com/a/35134329
    original_sigint_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)

    # Pipe used to send the data from the process reading the data to the process
    # sending to data via a post request.
    data_receiving_conn, data_sending_conn = Pipe()
    # Pipe used to tell the process reading the data to stop after sending the next
    # batch of data to the process sending the data
    kill_receiving_conn, kill_sending_conn = Pipe()

    p_read = Process(
        target=read_data,
        args=(
            settings.enviro_monitor.init_read_time,
            settings.enviro_monitor.resolution,
            settings.enviro_monitor.batch_size_send,
            settings.enviro_monitor.temp_calib_factor,
            data_sending_conn,
            kill_receiving_conn,
        ),
    )
    p_send = Process(
        target=send_data,
        args=(
            data_receiver_url,
            header,
            data_receiving_conn,
        ),
    )

    p_read.start()
    p_send.start()

    # Alter the processes are stated we reinstate the handler
    signal.signal(signal.SIGINT, original_sigint_handler)

    send_kill = False
    # This will run until KeyboardInterrupt is detercted
    while True:
        try:
            sleep(5)
        except KeyboardInterrupt:
            logger.warning("Set send_kill to True")
            send_kill = True

        if send_kill:
            logger.warning("send kill")
            kill_sending_conn.send(None)
            break

    logger.info("Waiting for processes to stop")
    while p_read.is_alive() or p_send.is_alive():
        logger.debug(
            "Reader alive: %s | Sender alive: %s", p_read.is_alive(), p_send.is_alive()
        )
        sleep(5)

    logger.info("Processes stopped. Exiting...")


if __name__ == "__main__":
    main()
