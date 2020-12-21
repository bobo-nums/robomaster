import logging
import multiprocessing as mp
import pickle
import queue
import threading
from typing import Tuple, List

import click
import cv2 as cv
import robomasterpy as rm
from pynput import keyboard
from pynput.keyboard import Key, KeyCode
from robomasterpy import CTX
from robomasterpy import framework as rmf

rm.LOG_LEVEL = logging.INFO
pickle.DEFAULT_PROTOCOL = pickle.HIGHEST_PROTOCOL

QUEUE_SIZE: int = 10
PUSH_FREQUENCY: int = 1
TIMEOUT_UNIT: float = 0.1
QUEUE_TIMEOUT: float = TIMEOUT_UNIT / PUSH_FREQUENCY


# just display the streaming video
def display(frame, **kwargs) -> None:
    cv.imshow("frame", frame)
    cv.waitKey(1)


def handle_event(cmd: rm.Commander, queues: Tuple[mp.Queue, ...], logger: logging.Logger) -> None:
    push_queue, event_queue = queues
    try:
        push = push_queue.get(timeout=QUEUE_TIMEOUT)
        logger.info('push: %s', push)
    except queue.Empty:
        pass

    try:
        event = event_queue.get(timeout=QUEUE_TIMEOUT)
        # safety first
        if type(event) == rm.ArmorHitEvent:
            cmd.chassis_speed(0, 0, 0)
        logger.info('event: %s', event)
    except queue.Empty:
        pass


class Controller:
    UNIT_DELTA_SPEED: float = 0.2
    UNIT_DELTA_DEGREE: float = 20

    def __init__(self, cmd: rm.Commander, logger: logging.Logger):
        self._mu = threading.Lock()
        with self._mu:
            self.gear: int = 1
            self.delta_v: float = self.UNIT_DELTA_SPEED
            self.delta_d: float = self.UNIT_DELTA_DEGREE
            self.cmd = cmd
            self.logger = logger
            self.v: List[float, float] = [0, 0]
            self.previous_v: List[float, float] = [0, 0]
            self.v_gimbal: List[float, float] = [0, 0]
            self.previous_v_gimbal: List[float, float] = [0, 0]
            self.ctrl_pressed: bool = False

    def on_press(self, key):
        with self._mu:
            self.logger.debug('pressed: %s', key)

            if key == Key.ctrl:
                self.ctrl_pressed = True
                return
            if self.ctrl_pressed and key == KeyCode(char='c'):
                # stop listener
                self.v = [0, 0]
                self.v_gimbal = [0, 0]
                self.send_command()
                return False
            if key == Key.space:
                self.cmd.blaster_fire()
                return

            if key == KeyCode(char='w'):
                self.v[0] = self.delta_v
            elif key == KeyCode(char='s'):
                self.v[0] = -self.delta_v
            elif key == KeyCode(char='a'):
                self.v[1] = -self.delta_v
            elif key == KeyCode(char='d'):
                self.v[1] = self.delta_v
            elif key == Key.up:
                self.v_gimbal[0] = self.delta_d
            elif key == Key.down:
                self.v_gimbal[0] = -self.delta_d
            elif key == Key.left:
                self.v_gimbal[1] = -self.delta_d
            elif key == Key.right:
                self.v_gimbal[1] = self.delta_d

            self.send_command()

    def _update_gear(self, gear: int):
        self.gear = gear
        self.delta_v = self.gear * self.UNIT_DELTA_SPEED
        self.delta_d = self.gear * self.UNIT_DELTA_DEGREE

    def on_release(self, key):
        with self._mu:
            self.logger.debug('released: %s', key)

            if key == Key.ctrl:
                self.ctrl_pressed = False
                return

            # gears
            if key in (KeyCode(char='1'), KeyCode(char='2'), KeyCode(char='3'), KeyCode(char='4'), KeyCode(char='5')):
                self._update_gear(int(key.char))
                return

            if key in (KeyCode(char='w'), KeyCode(char='s')):
                self.v[0] = 0
            elif key in (KeyCode(char='a'), KeyCode(char='d')):
                self.v[1] = 0
            elif key in (Key.up, Key.down):
                self.v_gimbal[0] = 0
            elif key in (Key.left, Key.right):
                self.v_gimbal[1] = 0

            self.send_command()

    def send_command(self):
        if self.v != self.previous_v:
            self.previous_v = [*self.v]
            self.logger.debug('chassis speed: x: %s, y: %s', self.v[0], self.v[1])
            self.cmd.chassis_speed(self.v[0], self.v[1], 0)
        if self.v_gimbal != self.previous_v_gimbal:
            self.logger.debug('gimbal speed: pitch: %s, yaw: %s', self.v_gimbal[0], self.v_gimbal[1])
            self.previous_v_gimbal = [*self.v_gimbal]
            self.cmd.gimbal_speed(self.v_gimbal[0], self.v_gimbal[1])


def control(cmd: rm.Commander, logger: logging.Logger, **kwargs) -> None:
    controller = Controller(cmd, logger)
    with keyboard.Listener(
            on_press=controller.on_press,
            on_release=controller.on_release) as listener:
        listener.join()


@click.command()
@click.option('--ip', default='', type=str, help='(Optional) IP of Robomaster EP')
@click.option('--timeout', default=10.0, type=float, help='(Optional) Timeout for commands')
def cli(ip: str, timeout: float):
    # manager is in charge of communicating among processes
    manager: mp.managers.SyncManager = CTX.Manager()

    with manager:
        # hub is the place to register your logic
        hub = rmf.Hub()
        cmd = rm.Commander(ip=ip, timeout=timeout)
        ip = cmd.get_ip()

        # initialize your Robomaster
        cmd.robot_mode(rm.MODE_GIMBAL_LEAD)
        cmd.gimbal_recenter()

        # enable video streaming
        cmd.stream(True)
        # rm.Vision is a handler for video streaming
        # display is the callback function defined above
        hub.worker(rmf.Vision, 'vision', (None, ip, display))

        # enable push and event
        cmd.chassis_push_on(PUSH_FREQUENCY, PUSH_FREQUENCY, PUSH_FREQUENCY)
        cmd.gimbal_push_on(PUSH_FREQUENCY)
        cmd.armor_sensitivity(10)
        cmd.armor_event(rm.ARMOR_HIT, True)
        cmd.sound_event(rm.SOUND_APPLAUSE, True)

        # the queues are where data flows
        push_queue = manager.Queue(QUEUE_SIZE)
        event_queue = manager.Queue(QUEUE_SIZE)

        # PushListener and EventListener handles push and event,
        # put parsed, well-defined data into queues.
        hub.worker(rmf.PushListener, 'push', (push_queue,))
        hub.worker(rmf.EventListener, 'event', (event_queue, ip))

        # Mind is the handler to let you bring your own controlling logic.
        # It can consume data from specified queues.
        hub.worker(rmf.Mind, 'event-handler', ((push_queue, event_queue), ip, handle_event))

        # a hub can have multiple Mind
        hub.worker(rmf.Mind, 'controller', ((), ip, control), {'loop': False})

        # Let's do this!
        hub.run()


if __name__ == '__main__':
    cli()