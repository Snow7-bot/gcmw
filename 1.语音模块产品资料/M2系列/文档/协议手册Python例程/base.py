#!/usr/bin/env python
# -*- coding: utf-8 -*-
import struct
import serial
import time
import sys
import re
import os
import subprocess
import threading
from pathlib import Path

#串口交互字段说明
record_set = '{"type":"dump_audio","content":{"debug":3}}'                  #0:停止记录音频/3:开始记录音频(原始和降噪音频)
clean_set = '{"type":"clean_pcm"}'                                          #清空音频
version_check = '{"type": "version"}'                                       #固件版本信息
manual_wakeup_set = '{"type": "manual_wakeup","content": {"beam": 1}}'      #手动唤醒设置波束1~6
mic6_set = '{"type":"switch_mic","content":{"mic":"mic6"}}'				    #线性六麦设置
mic_circle_set = '{"type":"switch_mic","content":{"mic":"mic6_circle"}}'    #环形六麦设置
#唤醒词设置(修改当前字段即可自定义唤醒词，默认设置格式为拼音+声调)
awake_word_set = '{"type": "wakeup_keywords", "content": { "keyword": "xiao3 wei1 xiao3 wei1", "threshold": "900"}}' 

class MicArrayController:
    def __init__(self, port, baudrate):
        self.ser = serial.Serial(port, baudrate)
        self.save_path = os.path.join(os.path.dirname(__file__), 'audio/')
        self.iat_pcm = '/data/build/iat.pcm'
        self.origin_pcm = '/data/build/origin.pcm'
        self.running = True
        self.feedback_thread = None
        print("M2模块已连接" if self.ser.isOpen() else "连接M2模块失败")

        # 启动反馈线程
        self.start_feedback_thread()

    #M2设备消息封装
    def Msg_Packet(self, Content):
        #消息内容
        Sync_Head = 0xA5
        User_ID = 0x01
        Msg_Type = 0x05
        MsgId_L = 0x01
        MsgId_H = 0x00
        MsgLen_Byte = len(Content).to_bytes(2, 'big')
        Msg_Len_L = MsgLen_Byte[1]
        Msg_Len_H = MsgLen_Byte[0]
        CheckCode = ((~sum([Sync_Head, User_ID, Msg_Type, Msg_Len_L, Msg_Len_H, MsgId_L, MsgId_H] + list(Content))) & 0xFF) +1

        #消息封装
        Sync_Head_ = Sync_Head.to_bytes(1, 'big')
        User_ID_ = User_ID.to_bytes(1, 'big')
        Msg_Type_ = Msg_Type.to_bytes(1, 'big')
        Msg_Len_L_ = Msg_Len_L.to_bytes(1, 'big')
        Msg_Len_H_ = Msg_Len_H.to_bytes(1, 'big')
        MsgId_L_ = MsgId_L.to_bytes(1, 'big')
        MsgId_H_ = MsgId_H.to_bytes(1, 'big')
        CheckCode_ = CheckCode.to_bytes(1, 'big')
        Msg = Sync_Head_ + User_ID_ + Msg_Type_ + Msg_Len_L_ + Msg_Len_H_ + MsgId_L_ + MsgId_H_ + Content + CheckCode_
        return Msg

    #类型切换
    def type_chage(self, string, num):
        return re.sub(r'\d+', str(num), string)
   
    #录音模式设置 
    def record_mode(self, status):
        if status == 0:
            Content = self.type_chage(record_set, status)
            self.ser.write(self.Msg_Packet(Content.encode()))
        elif status == 3:
            self.ser.write(self.Msg_Packet(clean_set.encode()))
            Content = self.type_chage(record_set, status)
            self.ser.write(self.Msg_Packet(Content.encode()))
        else:
            print("Unset mode!")

    #版本查询
    def version_check(self):
        self.ser.write(self.Msg_Packet(version_check.encode()))

    #麦克风类型切换
    def mic_type(self, type_num):
        if type_num == 0:
           self.ser.write(self.Msg_Packet(mic6_set.encode()))
        elif type_num == 1:
           self.ser.write(self.Msg_Packet(mic_circle_set.encode()))
        else:
           print("Type Set ERROR")

    #获取pcm音频
    def get_pcm(self):
        Path(self.save_path).mkdir(parents=True, exist_ok=True)
        try:
            origin_name = f"origin.pcm"
            subprocess.run(["adb", "pull", self.origin_pcm, os.path.join(self.save_path, origin_name)], check=True)
            iat_name = f"iat.pcm"
            subprocess.run(["adb", "pull", self.iat_pcm, os.path.join(self.save_path, iat_name)], check=True)
            print("音频文件拉取成功")
        except subprocess.CalledProcessError as e:
            print(f"命令执行错误: {e}")
    
    #删除音频
    def delete_audio(self, Path=None):
        file_path = Path or self.save_path
        if not os.path.exists(file_path):
            print(f"文件夹 '{file_path}' 不存在。")
            return
        
        for entry in os.scandir(file_path):  
            if entry.is_file() and entry.name.lower().endswith('.pcm'):
                try:
                    os.remove(entry.path)
                    print(f"已删除文件: {entry.path}")
                except Exception as e:
                    print(f"删除文件 {entry.path} 时出错: {e}")

    #设置波束
    def set_beam(self, beam):
        Content = self.type_chage(manual_wakeup_set, beam)
        self.ser.write(self.Msg_Packet(Content.encode()))
        print(f"切换到波束{beam}")
        return

    #设置唤醒词
    def set_awakeword(self):
        self.ser.write(self.Msg_Packet(awake_word_set.encode()))
    
    #串口反馈
    def feedback(self):
        while self.running:
            try:
                if self.ser.in_waiting > 0:
                    # 只检查第一个字节，如果不是同步头就跳过
                    first_byte = self.ser.read(1)
                    if first_byte[0] == 0xA5:
                        # 继续读取剩余头部
                        header = self.ser.read(6)  # user_id(1) + msg_type(1) + msg_len(2) + msg_id(2)
                        if len(header) == 6:
                            msg_type = header[1]
                            msg_len = int.from_bytes(header[2:4], 'little')
                            
                            # 读取消息内容和校验位
                            remaining = self.ser.read(msg_len + 1)  # content + checksum
                            if len(remaining) == msg_len + 1:
                                content = remaining[:msg_len]
                                checksum = remaining[msg_len]
                                
                                if msg_type == 0x04:
                                    try:
                                        print(f"设备反馈: {content.decode('utf-8')}")
                                    except:
                                        print(f"设备反馈(原始数据): {content.hex()}")
                    else:
                        # 如果不是同步头，继续读取直到找到同步头
                        continue
            except Exception as e:
                print(f"数据解析异常: {str(e)}")
                self.ser.reset_input_buffer()
            time.sleep(0.01)

    #启动反馈线程
    def start_feedback_thread(self):
        self.feedback_thread = threading.Thread(target=self.feedback)
        self.feedback_thread.daemon = True
        self.feedback_thread.start()
        print("串口反馈线程已启动")

    #停止所有线程
    def stop(self):
        self.running = False
        if self.feedback_thread and self.feedback_thread.is_alive():
            self.feedback_thread.join(timeout=1.0)
        if self.ser and self.ser.isOpen():
            self.ser.close()
        print("M2模块已停止串口交互")

def print_menu():
    print("\n===== M2模块键盘控制菜单 =====")
    print("1: 固件版本查询")
    print("2: 麦克风类型切换")
    print("3: 麦克风波束设置")
    print("4: 唤醒词设置")
    print("5: 获取降噪和原始音频")
    print("q: 退出程序")
    print("==========================")

def main():
    try:
        mic = MicArrayController('/dev/wheeltec_mic', 115200)
        
        while mic.running:
            print_menu()
            choice = input("请键盘输入指令: ").strip().lower()
            
            if choice == '1':
                mic.version_check()
            elif choice == '2':
                print("0: 线性六麦")
                print("1: 环形六麦")
                mic_type = input("请选择麦克风类型 (0/1): ").strip()
                if mic_type in ['0', '1']:
                    mic.mic_type(int(mic_type))
                else:
                    print("无效选择")
            elif choice == '3':
                beam = input("请设置波束 (0-5): ").strip()
                if beam in ['0', '1', '2', '3', '4', '5']:
                    mic.set_beam(int(beam))
                else:
                    print("无效波束值")
            elif choice == '4':
                mic.set_awakeword()
            elif choice == '5':
                print("开始录音5秒...")
                mic.record_mode(3)
                time.sleep(5)
                mic.record_mode(0)
                mic.get_pcm()
                print("音频获取完成")
            elif choice == 'q':
                print("正在退出程序...")
                mic.running = False
            else:
                print("无效选择，请重新输入")
            
            # 短暂等待，确保反馈信息能够显示
            time.sleep(0.5)
    
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序运行出错: {e}")
    finally:
        if 'mic' in locals():
            mic.stop()
        print("程序已终止")

if __name__ == "__main__":
    main()
