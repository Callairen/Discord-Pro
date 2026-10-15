from pypresence import Presence
import psutil
import time

CLIENT_ID = "1467854564458889519"

APP_PROCESS_NAMES = ["VirtualBox.exe", "VirtualBoxVM.exe"]

rpc = None
presence_active = False


def is_app_running(process_names):
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] in process_names:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False



def start_presence():
    global rpc, presence_active
    rpc = Presence(CLIENT_ID)
    rpc.connect()
    rpc.update(
        details="Pentesting Mode",
        state="Kali Linux on VirtualBox",
        large_image="kalinux",
        large_text="Kali Linux Lab",
        start=time.time()
    )
    presence_active = True
    print("Presence ON")


def stop_presence():
    global rpc, presence_active
    if rpc:
        rpc.clear()
        rpc.close()
    presence_active = False
    print("Presence OFF")


print("Kali Linux agent started...")

while True:
    app_running = is_app_running(APP_PROCESS_NAMES)

    if app_running and not presence_active:
        start_presence()

    elif not app_running and presence_active:
        stop_presence()

    time.sleep(5)
