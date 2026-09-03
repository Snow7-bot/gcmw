#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
串口消息读取和验证脚本 - Linux版本
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
    """串口消息读取器"""

    # 消息格式常量
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
        """打开串口"""
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
            print(f"无法打开串口: {self.port}")
            return False
        except serial.SerialException as e:
            print(f"串口打开失败: {e}")
            if "Permission denied" in str(e):
                print("提示: 需要使用sudo或添加用户到dialout组")
            return False

    def close_serial(self):
        """关闭串口"""
        if self.serial_conn and self.serial_conn.is_open:
            try:
                time.sleep(0.1)
                self.serial_conn.close()
                print("串口已关闭")
            except Exception as e:
                print(f"关闭串口时出错: {e}")

    @staticmethod
    def list_available_ports() -> List[str]:
        """列出所有可用的串口"""
        import glob
        ports = glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*') + \
                glob.glob('/dev/ttyS*') + glob.glob('/dev/tty.*')
        return sorted(ports)

    def calculate_checksum(self, data: bytes) -> int:
        """计算校验码"""
        checksum = sum(data) & 0xFF
        return ((~checksum) + 1) & 0xFF

    def encode_message(self, msg_type: int, payload: bytes, custom_msg_id: Optional[int] = None) -> bytes:
        """编码消息"""
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

    def send_handshake_ack(self, handshake_msg_id: int) -> bool:
        """发送握手确认"""
        if not self.serial_conn or not self.serial_conn.is_open:
            print("串口未打开，无法发送握手确认")
            return False
        try:
            handshake_ack_data = bytes([0xA5, 0x00, 0x00, 0x00])
            message = self.encode_message(
                self.HANDSHAKE_ACK_TYPE, handshake_ack_data, handshake_msg_id)
            
            with self.lock:
                written = self.serial_conn.write(message)
                self.serial_conn.flush()
            
            print(f"握手确认消息已发送 (ID: {handshake_msg_id}, 字节数: {written})")
            return True
        except Exception as e:
            print(f"发送握手确认失败: {e}")
            return False

    def parse_message_header(self, data: bytes) -> Optional[dict]:
        """解析消息头"""
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
        """验证消息格式与校验码"""
        if len(data) < self.HEADER_SIZE + 1:
            return False, None, None
        header_info = self.parse_message_header(data)
        if not header_info:
            return False, None, None

        if header_info['sync_header'] != self.SYNC_HEADER:
            print(f"无效同步头: 0x{header_info['sync_header']:02X}")
            return False, None, None
        if header_info['user_id'] != self.USER_ID:
            print(f"无效用户ID: 0x{header_info['user_id']:02X}")
            return False, None, None

        expected_total_length = self.HEADER_SIZE + \
            header_info['msg_length'] + 1
        if len(data) < expected_total_length:
            return False, None, None

        message_data = data[self.HEADER_SIZE:self.HEADER_SIZE +
                            header_info['msg_length']]
        received_checksum = data[self.HEADER_SIZE + header_info['msg_length']]
        expected_checksum = self.calculate_checksum(
            data[:self.HEADER_SIZE + header_info['msg_length']])

        if received_checksum != expected_checksum:
            print(
                f"校验码错误: 期望0x{expected_checksum:02X} 实际0x{received_checksum:02X}")
            return False, None, None

        return True, header_info, message_data

    def get_message_type_name(self, msg_type: int) -> str:
        """获取消息类型名"""
        mapping = {
            self.HANDSHAKE_MSG_TYPE: "握手消息",
            self.WAKEUP_MSG_TYPE: "设备消息",
            self.MANUAL_WAKEUP_TYPE: "手动唤醒",
            self.AUDIO_DATA_TYPE: "音频数据",
            self.HANDSHAKE_ACK_TYPE: "握手确认"
        }
        return mapping.get(msg_type, f"未知类型(0x{msg_type:02X})")

    def handle_wakeup_message(self, message_data: bytes):
        """处理设备唤醒消息"""
        try:
            text_data = message_data.decode('utf-8', errors='ignore')
            msg_json = json.loads(text_data)

            if msg_json.get("type") == "aiui_event":
                print("检测到设备唤醒消息:")
                print(json.dumps(msg_json, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"唤醒消息处理异常: {e}")

    def init_pcm_file(self):
        """初始化PCM文件"""
        try:
            with open(self.pcm_file, 'wb') as f:
                pass
            print(f"PCM文件已初始化: {self.pcm_file}")
        except Exception as e:
            print(f"初始化PCM文件失败: {e}")

    def handle_audio_data(self, message_data: bytes):
        """处理音频数据"""
        try:
            with open(self.pcm_file, 'ab') as f:
                f.write(message_data)
                if self.message_count % 10 == 0:
                    file_size = os.path.getsize(self.pcm_file)
                    print(f"音频数据已保存，当前PCM文件大小: {file_size} 字节")
        except Exception as e:
            print(f"保存音频数据失败: {e}")

    def send_get_original_audio(self):
        """发送获取原始音频消息"""
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
        """发送停止获取原始音频消息"""
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

    def send_manual_wakeup(self):
        """发送手动唤醒消息"""
        if not self.serial_conn or not self.serial_conn.is_open:
            print("串口未打开，无法发送消息")
            return False
        try:
            reply = {
                "type": "manual_wakeup",
                "content": {
                    "beam": 0
                }
            }
            reply_bytes = json.dumps(reply, ensure_ascii=False).encode('utf-8')
            message = self.encode_message(self.MANUAL_WAKEUP_TYPE, reply_bytes)
            with self.lock:
                written = self.serial_conn.write(message)
                self.serial_conn.flush()
            print(f"已发送手动唤醒消息 (0x05) 长度: {len(reply_bytes)} 字节, 已发送: {written} 字节")
            return True
        except Exception as e:
            print(f"发送手动唤醒消息失败: {e}")
            return False

    def send_wakeup_keywords(self):
        """发送唤醒关键词消息"""
        if not self.serial_conn or not self.serial_conn.is_open:
            print("串口未打开，无法发送消息")
            return False
        try:
            wakeup_keywords_msg = {
                "type": "wakeup_keywords",
                "content": {
                    "keyword": "小微小微",
                    "threshold": "900"
                }
            }
            wakeup_keywords_bytes = json.dumps(
                wakeup_keywords_msg, ensure_ascii=False).encode('utf-8')
            wakeup_keywords_message = self.encode_message(
                self.MANUAL_WAKEUP_TYPE, wakeup_keywords_bytes)
            with self.lock:
                written = self.serial_conn.write(wakeup_keywords_message)
                self.serial_conn.flush()
            print(f"已发送唤醒关键词消息 (0x05) 长度: {len(wakeup_keywords_bytes)} 字节, 已发送: {written} 字节")
            return True
        except Exception as e:
            print(f"发送唤醒关键词消息失败: {e}")
            return False

    def send_get_version(self):
        """发送获取版本消息"""
        if not self.serial_conn or not self.serial_conn.is_open:
            print("串口未打开，无法发送消息")
            return False
        try:
            version_msg = {
                "type": "version",
            }
            version_bytes = json.dumps(version_msg, ensure_ascii=False).encode('utf-8')
            version_message = self.encode_message(self.MANUAL_WAKEUP_TYPE, version_bytes)
            with self.lock:
                written = self.serial_conn.write(version_message)
                self.serial_conn.flush()
            print(f"已发送获取版本消息 (0x05) 长度: {len(version_bytes)} 字节, 已发送: {written} 字节")
            return True
        except Exception as e:
            print(f"发送获取版本消息失败: {e}")
            return False

    def handle_keyboard_input(self):
        """处理键盘输入"""
        print("\n键盘控制说明:")
        print("  0 - 停止获取原始音频")
        print("  1 - 发送获取原始音频消息")
        print("  2 - 发送手动唤醒消息")
        print("  3 - 发送唤醒关键词消息")
        print("  4 - 发送获取版本消息")
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
        """处理键盘按键"""
        if key == 'q':
            print("\n正在退出...")
            self.running = False
        elif key == '0':
            self.send_stop_original_audio()
        elif key == '1':
            self.send_get_original_audio()
        elif key == '2':
            self.send_manual_wakeup()
        elif key == '3':
            self.send_wakeup_keywords()
        elif key == '4':
            self.send_get_version()
        elif key:
            print(f"未知按键: {key}，请按 0/1/2/3/4/q")

    def print_message_info(self, header_info: dict, message_data: bytes):
        """打印消息内容"""
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
                else:
                    print(f"数据(二进制): {len(message_data)} 字节")
            except Exception as e:
                print(f"数据(二进制): {len(message_data)} 字节")

    def read_messages(self):
        """持续读取消息"""
        if not self.serial_conn or not self.serial_conn.is_open:
            print("串口未打开")
            return

        self.init_pcm_file()

        keyboard_thread = threading.Thread(
            target=self.handle_keyboard_input, daemon=True)
        keyboard_thread.start()

        print(f"开始监听 {self.port} ... (按 'q' 退出)")
        buffer = b''
        last_activity_time = time.time()

        try:
            while self.running:
                try:
                    if self.serial_conn.in_waiting > 0:
                        bytes_to_read = min(self.serial_conn.in_waiting, 4096)
                        data = self.serial_conn.read(bytes_to_read)
                        if data:
                            buffer += data
                            last_activity_time = time.time()
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
                        discarded = buffer[:sync_pos]
                        if len(discarded) > 0:
                            print(f"丢弃 {len(discarded)} 字节无效数据")
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

                        if header_info['msg_type'] == self.HANDSHAKE_MSG_TYPE:
                            print(f"检测到握手消息 -> 自动回复握手确认 (ID: {header_info['msg_id']})")
                            self.send_handshake_ack(header_info['msg_id'])
                        elif header_info['msg_type'] == self.WAKEUP_MSG_TYPE:
                            self.handle_wakeup_message(msg_data)
                        elif header_info['msg_type'] == self.AUDIO_DATA_TYPE:
                            self.handle_audio_data(msg_data)
                    else:
                        print("无效消息，已跳过。")
                        buffer = buffer[1:] if len(buffer) > 0 else b''
                        continue

                time.sleep(0.001)
                
                if time.time() - last_activity_time > 5.0 and len(buffer) > 0:
                    print(f"缓冲区中有 {len(buffer)} 字节未处理数据，等待更多数据...")
                    last_activity_time = time.time()

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
        description='串口消息读取与验证工具 - Linux版本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s -p /dev/ttyUSB0 -b 115200
  %(prog)s --list-ports
        """
    )
    parser.add_argument('-p', '--port', 
                       default='/dev/ttyCH343USB0',
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
    
    max_retries = 3
    for attempt in range(max_retries):
        print(f"尝试打开串口 {args.port} (尝试 {attempt + 1}/{max_retries})...")
        if reader.open_serial():
            break
        if attempt < max_retries - 1:
            time.sleep(1)
    else:
        print(f"无法打开串口 {args.port}，程序退出")
        sys.exit(1)
    
    try:
        reader.read_messages()
    finally:
        reader.close_serial()


if __name__ == '__main__':
    main()
