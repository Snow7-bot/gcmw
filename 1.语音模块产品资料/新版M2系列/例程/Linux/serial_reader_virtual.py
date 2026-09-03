#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
串口消息读取和验证脚本 - Linux虚拟串口版本
"""

import serial
import struct
import time
import sys
import argparse
import json
import threading
import select
import os
from typing import Optional, Tuple, List


class SerialMessageReader:
    """串口消息读取器 - 虚拟串口版本"""

    SYNC_HEADER = 0xA5
    USER_ID = 0x01
    WAKEUP_MSG_TYPE = 0x04
    MANUAL_WAKEUP_TYPE = 0x05
    AUDIO_DATA_TYPE = 0x06
    HANDSHAKE_MSG_TYPE = 0x01
    HANDSHAKE_ACK_TYPE = 0xFF
    HEADER_SIZE = 7
    MAX_MESSAGE_SIZE = 1024

    def __init__(self, port: str, baudrate: int = 115200, pcm_file: str = 'audio.pcm'):
        self.port = port
        self.baudrate = baudrate
        self.serial_conn: Optional[serial.Serial] = None
        self.message_count = 0
        self.message_id = 0
        self.pcm_file = pcm_file
        self.running = True
        self.lock = threading.Lock()

    def open_serial(self) -> bool:
        try:
            self.serial_conn = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.5,
                write_timeout=2.0
            )
            
            if self.serial_conn.is_open:
                print(f"串口已打开: {self.port}, 波特率: {self.baudrate}")
                self.serial_conn.reset_input_buffer()
                self.serial_conn.reset_output_buffer()
                time.sleep(0.5)
                return True
            return False
        except serial.SerialException as e:
            print(f"串口打开失败: {e}")
            return False

    def close_serial(self):
        if self.serial_conn and self.serial_conn.is_open:
            try:
                time.sleep(0.1)
                self.serial_conn.close()
                print("串口已关闭")
            except Exception as e:
                print(f"关闭串口时出错: {e}")

    @staticmethod
    def list_available_ports() -> List[str]:
        import glob
        ports = glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*') + \
                glob.glob('/dev/ttyS*') + glob.glob('/dev/tty.*')
        return sorted(ports)

    def calculate_checksum(self, data: bytes) -> int:
        checksum = sum(data) & 0xFF
        return ((~checksum) + 1) & 0xFF

    def encode_message(self, msg_type: int, payload: bytes, custom_msg_id: Optional[int] = None) -> bytes:
        payload_len = len(payload)
        total_size = self.HEADER_SIZE + payload_len + 1
        message = bytearray(total_size)
        offset = 0

        message[offset] = self.SYNC_HEADER
        offset += 1
        message[offset] = self.USER_ID
        offset += 1
        message[offset] = msg_type
        offset += 1
        message[offset:offset + 2] = struct.pack('<H', payload_len)
        offset += 2
        msg_id = custom_msg_id if custom_msg_id is not None else self.message_id
        message[offset:offset + 2] = struct.pack('<H', msg_id)
        offset += 2
        message[offset:offset + payload_len] = payload
        offset += payload_len
        message[offset] = self.calculate_checksum(message[:offset])

        if custom_msg_id is None:
            self.message_id = (self.message_id + 1) % 65536
        return bytes(message)

    def send_get_original_audio(self):
        if not self.serial_conn or not self.serial_conn.is_open:
            print("串口未打开，无法发送消息")
            return False
        try:
            reply = {
                "type": "get_original_audio",
                "content": {
                    "audio": 1
                }
            }
            reply_bytes = json.dumps(reply, ensure_ascii=False).encode('utf-8')
            message = self.encode_message(self.MANUAL_WAKEUP_TYPE, reply_bytes)
            with self.lock:
                written = self.serial_conn.write(message)
                self.serial_conn.flush()
            print(f"已发送获取原始音频消息 (0x05) 长度: {len(reply_bytes)} 字节, 已发送: {written} 字节")
            return True
        except Exception as e:
            print(f"发送获取原始音频消息失败: {e}")
            return False

    def send_stop_original_audio(self):
        if not self.serial_conn or not self.serial_conn.is_open:
            print("串口未打开，无法发送消息")
            return False
        try:
            reply = {
                "type": "get_original_audio",
                "content": {
                    "audio": 0
                }
            }
            reply_bytes = json.dumps(reply, ensure_ascii=False).encode('utf-8')
            message = self.encode_message(self.MANUAL_WAKEUP_TYPE, reply_bytes)
            with self.lock:
                written = self.serial_conn.write(message)
                self.serial_conn.flush()
            print(f"已发送停止获取原始音频消息 (0x05) 长度: {len(reply_bytes)} 字节, 已发送: {written} 字节")
            return True
        except Exception as e:
            print(f"发送停止获取原始音频消息失败: {e}")
            return False

    def handle_keyboard_input(self):
        print("\n键盘控制说明:")
        print("  1 - 发送获取原始音频消息")
        print("  0 - 停止获取原始音频")
        print("  q - 退出程序")
        print("=" * 50)

        try:
            while self.running:
                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                if ready:
                    line = sys.stdin.readline().strip()
                    if line:
                        self.process_key_input(line.lower())
        except Exception as e:
            print(f"键盘输入处理异常: {e}")
            self.running = False

    def process_key_input(self, key: str):
        if key == 'q':
            print("\n正在退出...")
            self.running = False
        elif key == '1':
            self.send_get_original_audio()
        elif key == '0':
            self.send_stop_original_audio()
        elif key:
            print(f"未知按键: {key}，请按 1/0/q")

    def parse_message_header(self, data: bytes) -> Optional[dict]:
        if len(data) < self.HEADER_SIZE:
            return None
        try:
            return {
                'sync_header': data[0],
                'user_id': data[1],
                'msg_type': data[2],
                'msg_length': struct.unpack('<H', data[3:5])[0],
                'msg_id': struct.unpack('<H', data[5:7])[0]
            }
        except struct.error:
            return None

    def validate_message(self, data: bytes) -> Tuple[bool, Optional[dict], Optional[bytes]]:
        if len(data) < self.HEADER_SIZE + 1:
            return False, None, None
        header_info = self.parse_message_header(data)
        if not header_info:
            return False, None, None

        if header_info['sync_header'] != self.SYNC_HEADER:
            return False, None, None
        if header_info['user_id'] != self.USER_ID:
            return False, None, None

        expected_total_length = self.HEADER_SIZE + header_info['msg_length'] + 1
        if len(data) < expected_total_length:
            return False, None, None

        message_data = data[self.HEADER_SIZE:self.HEADER_SIZE + header_info['msg_length']]
        received_checksum = data[self.HEADER_SIZE + header_info['msg_length']]
        expected_checksum = self.calculate_checksum(
            data[:self.HEADER_SIZE + header_info['msg_length']])

        if received_checksum != expected_checksum:
            return False, None, None

        return True, header_info, message_data

    def get_message_type_name(self, msg_type: int) -> str:
        mapping = {
            self.HANDSHAKE_MSG_TYPE: "握手消息",
            self.WAKEUP_MSG_TYPE: "设备消息",
            self.MANUAL_WAKEUP_TYPE: "手动唤醒",
            self.AUDIO_DATA_TYPE: "音频数据",
            self.HANDSHAKE_ACK_TYPE: "握手确认"
        }
        return mapping.get(msg_type, f"未知类型(0x{msg_type:02X})")

    def handle_audio_data(self, message_data: bytes):
        try:
            with open(self.pcm_file, 'ab') as f:
                f.write(message_data)
                if self.message_count % 10 == 0:
                    file_size = os.path.getsize(self.pcm_file)
                    print(f"音频数据已保存，当前PCM文件大小: {file_size} 字节")
        except Exception as e:
            print(f"保存音频数据失败: {e}")

    def print_message_info(self, header_info: dict, message_data: bytes):
        self.message_count += 1
        print(f"\n--- 消息 #{self.message_count} ---")
        print(f"类型: {self.get_message_type_name(header_info['msg_type'])}")
        print(f"ID: {header_info['msg_id']}")
        print(f"长度: {header_info['msg_length']} 字节")

        if message_data:
            if header_info['msg_type'] == self.AUDIO_DATA_TYPE:
                print(f"音频数据: {len(message_data)} 字节")
                return
            
            try:
                text_data = message_data.decode('utf-8', errors='ignore')
                if any(c.isprintable() for c in text_data):
                    display_text = text_data[:100] + "..." if len(text_data) > 100 else text_data
                    print(f"数据(文本): {display_text}")
            except Exception:
                pass

    def read_messages(self):
        if not self.serial_conn or not self.serial_conn.is_open:
            print("串口未打开")
            return

        keyboard_thread = threading.Thread(
            target=self.handle_keyboard_input, daemon=True)
        keyboard_thread.start()

        print(f"开始监听 {self.port} ... (按 'q' 退出)")
        buffer = b''

        try:
            while self.running:
                try:
                    if self.serial_conn.in_waiting > 0:
                        bytes_to_read = min(self.serial_conn.in_waiting, 4096)
                        data = self.serial_conn.read(bytes_to_read)
                        if data:
                            buffer += data
                except (OSError, serial.SerialException) as e:
                    print(f"读取串口数据时出错: {e}")
                    time.sleep(0.1)
                    continue

                while len(buffer) >= self.HEADER_SIZE and self.running:
                    sync_pos = buffer.find(bytes([self.SYNC_HEADER]))
                    if sync_pos == -1:
                        buffer = b''
                        break
                    
                    if sync_pos > 0:
                        buffer = buffer[sync_pos:]

                    header_info = self.parse_message_header(buffer)
                    if not header_info:
                        buffer = buffer[1:] if len(buffer) > 1 else b''
                        continue

                    total_len = self.HEADER_SIZE + header_info['msg_length'] + 1
                    if len(buffer) < total_len:
                        break

                    message = buffer[:total_len]
                    buffer = buffer[total_len:]

                    valid, header_info, msg_data = self.validate_message(message)
                    if valid:
                        self.print_message_info(header_info, msg_data)

                        if header_info['msg_type'] == self.AUDIO_DATA_TYPE:
                            self.handle_audio_data(msg_data)
                    else:
                        buffer = buffer[1:] if len(buffer) > 0 else b''
                        continue

                time.sleep(0.001)

        except KeyboardInterrupt:
            print(f"\n收到中断信号，正在退出...")
            self.running = False
        except Exception as e:
            print(f"读取错误: {e}")
            self.running = False
        finally:
            print(f"\n退出，共读取 {self.message_count} 条消息。")


def main():
    parser = argparse.ArgumentParser(
        description='串口消息读取与验证工具 - Linux虚拟串口版本',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('-p', '--port', 
                       default='/dev/ttyACM0',
                       help='串口设备路径 (默认: /dev/ttyACM0)')
    parser.add_argument('-b', '--baudrate', type=int,
                       default=115200, help='波特率 (默认: 115200)')
    parser.add_argument('-o', '--output', default='audio.pcm',
                       help='PCM音频输出文件路径 (默认: audio.pcm)')
    parser.add_argument('-l', '--list-ports', action='store_true',
                       help='列出所有可用的串口设备')
    
    args = parser.parse_args()

    if args.list_ports:
        print("扫描可用串口设备...")
        ports = SerialMessageReader.list_available_ports()
        if ports:
            print("找到以下串口:")
            for i, port in enumerate(ports, 1):
                print(f"  {i}. {port}")
        else:
            print("未找到可用的串口设备")
        return

    reader = SerialMessageReader(args.port, args.baudrate, args.output)
    
    if reader.open_serial():
        try:
            reader.read_messages()
        finally:
            reader.close_serial()
    else:
        print(f"无法打开串口 {args.port}，程序退出")
        sys.exit(1)


if __name__ == '__main__':
    main()
