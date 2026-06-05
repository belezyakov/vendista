import threading
import logging
from time import time, sleep
from serial import Serial, SerialException, PARITY_NONE, STOPBITS_ONE, EIGHTBITS
import struct
from queue import Queue
import binascii
from dataclasses import dataclass
from enum import Enum
import libscrc
from typing import List, Tuple

logger = logging.getLogger("vendista")

class IntEnum(int, Enum):
    description: str

    def __int__(self):
        return self.value

    def __bytes__(self):
        return self.value.to_bytes(1)

    def __str__(self):
        return self.description

    def __eq__(self, other):
        return self.value == other
    
    def __ne__(self, other):
        return self.value != other

    def __new__(cls, value: int, description: str = ""):
        obj = int.__new__(cls, value)
        obj._value_ = value
        obj.description = description
        return obj
    
class Type(IntEnum):
    READ_CARD = 0x01, "Read card"
    PACKET_FROM_SERVER = 0x02, "Packet from server"
    SHOW_PICTURE = 0x03, "Show picture"
    SHOW_QR = 0x04, "Show QR"
    REBOOT = 0x05, "Reboot"
    PING_SERVER = 0x06, "Ping server"
    CANCEL_LAST_TRANSACTION = 0x07, "Cancel last transaction" # Отменить последнюю транзакцию (оплату с карты). В ответ терминал вернет пакет PacketToServer
    CANCEL_READ_CARD = 0x08, "Cancel read card"     # Отменить команду ReadCard
    FILL_SCREEN = 0x09, "Fill screen"               # Очистить экран (заполнить одним цветом)
    WRITE_LINE = 0x0A, "Write line"                 # Вывести на экран строку текста
    SERVER_CONNECT_STATE = 0x0B, "Server connect state" # Уведомить Slave о состоянии соединения с сервером (актуально только для режима Slave + MDB)
    SET_CUSTOM_VALUE = 0x0C, "Set custom value"     # Установить значение внутренней переменной в Slave
    VEND_REPORT = 0x0E, "Vend report"               # Зарегистрировать продажу (нал/безнал) с формированием фискального чека в онлайн кассе
    PACKET_TO_EXT_SERVER = 0x0F, "Custom packet to external server" # Отправить на сервер произвольный пакет (для пересылки на Внешний сервер Master).  В ответ Slave отправит пакет на свой сервер препроцессинга (если у него есть своя симка), и вернет на Master этот же пакет через PacketToServer
    CONNECT_STATE_REQUEST = 0x10, "Server connect state request" # Запросить состояние соединения Slave со своим сервером процессинга. Актуально при использовании симки Slave
    TOUCH = 0x11, "Touch"                           # Уведомление о нажатии на LCD
    PACKET_TO_SERVER = 0x12, "Packet to server"     # Инструкция для Master - отправить пакет на сервер
    CARD_READ_RESULT = 0x13, "Card read result"     # Результат поиска и чтения карты
    CARD_AUTH_RESULT = 0x14, "Card authorization result" # Результат авторизации карты: оплата отклонена или принята
    ACK = 0x15, "Ack"                               # Подтверждение приема пакета от Master
    REBOOT_REPORT = 0x16, "Reboot report"           # Уведомление о перезагрузке
    CONNECT_STATE_REPORT = 0x18, "Server connect state report" # Уведомление о состоянии соединения Slave со своим сервером процессинга
    PACKET_TO_MASTER = 0x1B, "Custom packet to master" # Передача произвольного пакета данных пользователя на Master
    WRITE_TEXT_RECT = 0x30, "Write text and rectangle" # Предназначена для пакетной отрисовки примитивов(прямоугольников) и вывода текста
    VEND_REQUEST = 0x31, "Vend request"             # Запрос на продажу для получения QR на оплату (QR СБП)

class Picture(IntEnum):
    PRESS_KEY_FOR_PAYMENT = 1, "Press key for payment"   # Нажмите кнопку для оплаты картой
    SELECT_GOODS = 2, "Select goods"                # Выберите товар на автомате
    AUTHORIZATION = 4, "Authorization"              # Авторизация 
    SUCCESS = 5, "Success"                          # Успешно 
    REJECTED = 6, "Rejected"                        # Отклонено  
    UNAVAILABLE = 8, "Unavailable"                  # Недоступно 
    WAIT = 9, "Wait"                                # Ожидайте 
    ERROR = 12, "Error"                             # Ошибка 
    CUSTOM = 13, "Custom"                           # Кастомная картинка 
    CANCEL = 14, "Cancel"                           # Отмена последней транзакции успешно выполнена (деньги вернулись на карту) 

class ConnectState(IntEnum):
    NONE = 0, "None"
    DISCONNECTED = 1, "Disconnected"
    SIM800_FOUND = 2, "SIM800 found"
    SIM800_NOT_FOUND = 3, "SIM800 not found"
    SIM_FOUND = 4, "SIM found"
    REG_PASSED = 5, "GSM reg passed"
    PHONE_NUM_ACQ = 6, "Phone num acquired"
    GPRS_ACTIVE = 7, "GPRS active"
    IP_ACQ = 8, "IP acquired"
    SERVER_CONNECTED = 9, "Server connected"

class Currency(IntEnum): # ISO 4217
    RUB = 643, "RUB"
    USD = 840, "USD"
    EUR = 978, "EUR"
    
    def bcd(self):
        bcd_value = 0
        multiplier = 1
        number = self.value
        while number > 0:
            digit = number % 10
            bcd_value += digit * multiplier
            multiplier *= 16
            number //= 10
        lower_byte = (bcd_value & 0x00FF) << 8
        upper_byte = (bcd_value & 0xFF00) >> 8
        return lower_byte | upper_byte

class Vendista(object):
    header_format = struct.Struct('<HH')
    packet_read_card = struct.Struct('<LHLB')
    packet_show_qr = struct.Struct('<pp')
    # packet_vend_report = struct.Struct('<BHBB')
    # NOTE: not working format above for 0x0E command, 
    # use VendReportMulti format instead even for single product
    packet_vend_report_cashless = struct.Struct('<B')
    packet_vend_report_product = struct.Struct('<IH')
    packet_vend_report_gsm_flag = struct.Struct('<B')
    packet_vend_request = struct.Struct('<IHB')
    packet_fill_screen = struct.Struct('<H')
    packet_write_line = struct.Struct('<HHBHHp')
    packet_card_read_result = struct.Struct('<B8s')

    def __init__(self, serial_port, event_queue: Queue, log_level=logging.INFO):
        self.serial_port = serial_port
        self.event_queue = event_queue
        logger.setLevel(log_level)

        self.port: Serial | None = None
        self.lock = threading.Lock()
        self.pending_payment = None
        self.connect_state = ConnectState.NONE
        self.connection_error = False
        self.last_check_time = None
        self.error_counter = 10

        self.stop_request = threading.Event()
        vendista_loop_thread = threading.Thread(target=self.vendista_loop, daemon=True, name="vendista")
        vendista_loop_thread.start()

    def open(self):
        if self.port and self.port.is_open:
            return
        self.port = Serial(
            port = self.serial_port, 
            baudrate = 115200,
            parity = PARITY_NONE,
            stopbits = STOPBITS_ONE,
            bytesize = EIGHTBITS,
            timeout = 1.0
        )
        logger.info(f"Port {self.serial_port} opened")
        self.show_picture(Picture.WAIT)

    def close(self):
        if not self.stop_request.is_set():
            self.stop_request.set()
            logger.info("Stop request")
        if self.port and self.port.is_open:
            self.port.close()
            logger.info("Port closed")
        self.port = None

    def vendista_loop(self):
        while not self.stop_request.is_set():
            try:
                self.open()
                while not self.stop_request.is_set():
                    # Trying to receive
                    with self.lock:
                        received = self.port.read_all()
                    if received is not None:
                        # Parse events
                        received = bytearray(received)
                        while len(received) > 0:
                            result = self.decode(received)
                            if result is not None:
                                self.event(result[0], result[1])
                            else:
                                self.get_connect_state()
                    # Check pending transaction
                    if self.pending_payment is not None:
                        elapsed = time() - self.pending_payment.get("time", 0)
                        timeout = 70 if self.pending_payment.get("active", False) else 50
                        if elapsed > timeout:
                            self.close_payment()
                    # Check connection status
                    if self.error_counter == 0 and self.connect_state == ConnectState.NONE:
                        self.connect_state = ConnectState.DISCONNECTED
                        logger.debug("Error counter is 0 while state is NONE")
                    elif self.error_counter > 5 and self.connect_state > ConnectState.NONE:
                        self.connect_state = ConnectState.NONE
                        logger.error(f"Connect state: {self.connect_state}")
                    if self.last_check_time is None or time() - self.last_check_time > 10:
                        self.last_check_time = time()
                        self.get_connect_state()
                    # Prevent sleep of terminal
                    if self.last_request_time is not None and time() - self.last_request_time > 10*60:
                        if self.connect_state == ConnectState.SERVER_CONNECTED:
                            self.show_picture(Picture.SELECT_GOODS)
                        else:
                            self.show_picture(Picture.UNAVAILABLE)
                    sleep(0.5)
            except SerialException as e:
                if self.lock.locked():
                    self.lock.release()
                self.connect_state = ConnectState.NONE
                logger.error(f"Error opening or communicating with serial port: {e}")
                sleep(1)
        self.close() 
        logger.info("Stopped")

    def request(self, packet_type: Type, data: bytes | None = None):
        if data is not None:
            body = bytes(packet_type) + data
        else:
            body = bytes(packet_type)
        logger.debug(f"Send packet: {str(packet_type)}, {body.hex(' ').upper()}")
        body_length = len(body)
        crc16 = libscrc.hacker16(body, poly=0x8005, init=0xFFFF, xorout=0xFFFF, refin=False, refout=False)
        packet = self.header_format.pack(body_length, crc16) + body
        self.last_request_time = time()
        try:
            self.open()
            self.lock.acquire()
            self.port.write(packet)
            received = bytearray(self.port.read(128))
            self.lock.release()
            if len(received):
                response = self.decode(received)
                if response is not None:
                    (packet_type, body) = response
                    if packet_type == Type.ACK:
                        self.error_counter = 0
                        logger.debug(f"Response: {str(packet_type)}")
                        while len(received) > 0:
                            result = self.decode(received)
                            if result is not None:
                                self.event(result[0], result[1])                        
                        return packet_type
                    logger.debug(f"Response: {str(packet_type)}, {body.hex(' ').upper()}")
                    while len(received) > 0:
                        result = self.decode(received)
                        if result is not None:
                            self.event(result[0], result[1])                    
                    return packet_type, body
                else:
                    if self.error_counter < 10:
                        self.error_counter += 1 
                    logger.debug(f"Bad response: {received.hex(' ').upper()}")
            else:
                if self.error_counter < 10:
                    self.error_counter += 1 
                logger.debug("No response")
        except SerialException as e:
            self.lock.release()
            if self.error_counter < 10:
                self.error_counter += 1
            logger.error(f"Error opening or communicating with serial port: {e}")

    def decode(self, data: bytearray):
        if len(data) < 5:
            logger.debug(f"Invalid packet: {data.hex(' ').upper()}")
            return
        body_length, crc16 = self.header_format.unpack_from(data)
        packet_len = body_length + 4
        if len(data) < packet_len:
            logger.debug(f"Invalid length: {data.hex(' ').upper()}")
            return
        body = data[4:packet_len]
        del data[:packet_len]
        body_crc16 = libscrc.hacker16(body, poly=0x8005, init=0xFFFF, xorout=0xFFFF, refin=False, refout=False)
        if body_crc16 != crc16:
            logger.debug("Invalid crc")
            return
        try:
            packet_type = Type(body[0])
        except ValueError:
            logger.debug(f"Unknown type: {body[0]}")
            return
        return packet_type, bytes(body[1:])
    
    def event(self, packet_type: Type, data: bytes):
        if packet_type == Type.TOUCH:
            self.event_queue.put(dict(
                sender = self, 
                type = packet_type))
        elif packet_type == Type.CARD_READ_RESULT:
            result, card = self.packet_card_read_result.unpack_from(data)
            if result == 0x00:
                logger.debug("Card not found")
                if self.pending_payment is not None:
                    self.pending_payment["time"] = time()
                    self.pending_payment["active"] = False
            elif result == 0x01:
                logger.debug("Card found, but not readed")
                if self.pending_payment is not None:
                    self.pending_payment["time"] = time()
                    self.pending_payment["active"] = False
            elif result == 0x02:
                card_number = binascii.hexlify(bytes(card)).decode()
                card_number = card_number[:4] + "********" + card_number[12:]
                logger.debug(f"Card {card_number} readed")
                if type(self.pending_payment) == dict:
                    self.pending_payment["card_number"] = card_number
                    self.pending_payment["time"] = time()
                    self.pending_payment["active"] = False
        elif packet_type == Type.CARD_AUTH_RESULT:
            if len(data) == 1:
                if data[0] == 0x01:
                    if (self.pending_payment is not None):
                        amount = self.pending_payment.get("amount", 0)
                        card_number = self.pending_payment.get("card_number", "")
                        logger.debug(f"Card auth ok")
                        self.event_queue.put(dict(
                            sender = self,
                            type = packet_type,
                            amount = amount,
                            card_number = card_number))
                else:
                    logger.error(f"Card auth fail")
            else:
                logger.debug(f"Card auth len {len(data)}")
            if self.pending_payment is not None:
                self.pending_payment["time"] = time()
                self.pending_payment["active"] = False
        elif packet_type == Type.CONNECT_STATE_REPORT:
            if len(data) == 1:
                try:
                    new_connect_state = ConnectState(data[0])
                except ValueError:
                    new_connect_state = ConnectState.NONE
                    logger.debug(f"Unknown connect state: {data[0]}")
                self.error_counter = 0
                if new_connect_state != self.connect_state:
                    if self.connection_error and new_connect_state == ConnectState.SERVER_CONNECTED:
                        # Соединение восстановлено
                        self.connection_error = False
                        self.event_queue.put(dict(
                            sender = self,
                            type = packet_type,
                            state = new_connect_state))
                    elif self.connect_state == ConnectState.SERVER_CONNECTED:
                        # Соединение потеряно
                        self.connection_error = True
                        self.event_queue.put(dict(
                            sender = self,
                            type = packet_type,
                            state = new_connect_state))
                    self.connect_state = new_connect_state
                    logger.info(f"Connect state: {new_connect_state}")
                    if new_connect_state == ConnectState.SERVER_CONNECTED:
                        self.show_picture(Picture.SELECT_GOODS)
                    else:
                        self.show_picture(Picture.UNAVAILABLE)

    def show_picture(self, id):
        result = self.request(Type.SHOW_PICTURE, id.to_bytes(1))
        return result == Type.ACK

    def if_connected_to_server(self):
        return self.connect_state == ConnectState.SERVER_CONNECTED

    def get_connect_state(self):
        result = self.request(Type.CONNECT_STATE_REQUEST)
        return result == Type.ACK

    def read_card(self, sum, currency=Currency.RUB):
        logger.info(f"Read card request, {sum} {currency}")
        self.pending_payment = { "amount": sum, "active": True, "time": time() }
        unix_time = 0
        body = self.packet_read_card.pack(int(sum * 100), currency.bcd(), unix_time, 1)
        result = self.request(Type.READ_CARD, body)
        return result == Type.ACK

    def cancel_read_card(self):
        logger.info("Cancel read card")
        self.request(Type.CANCEL_READ_CARD)
        self.close_payment()

    def cancel_last_transaction(self):
        logger.info("Cancel last transaction")
        result = self.request(Type.CANCEL_LAST_TRANSACTION)
        if self.pending_payment is not None:
            self.pending_payment["time"] = time()
            self.pending_payment["active"] = False
        return result == Type.ACK

    def vend_report(self, products_list: List[Tuple[int, int]]):
        logger.info(f"Vend report, {products_list}")
        body = self.packet_vend_report_cashless.pack(2) # Cashless
        for product_number, product_price in products_list:
            body += self.packet_vend_report_product.pack(product_price, product_number)
        body += self.packet_vend_report_gsm_flag.pack(1) # Use terminal gsm
        result = self.request(Type.VEND_REPORT, body)
        return result == Type.ACK

    def vend_request(self, sum):
        logger.info(f"Vend request, {sum}")
        self.pending_payment = { "amount": sum, "active": True, "time": time() }
        body = self.packet_vend_request.pack(sum * 100, 1, 1)
        self.request(Type.VEND_REQUEST, body)

    def close_payment(self):
        if self.pending_payment is not None:
            self.pending_payment = None
            logger.debug("Close payment")
        if self.connect_state == ConnectState.SERVER_CONNECTED:
            self.show_picture(Picture.SELECT_GOODS)
        else:
            self.show_picture(Picture.UNAVAILABLE)
