import socket
import threading

HOST = '127.0.0.1'
PORT = 12345

nickname = input("Choose your nickname: ")

client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client.connect((HOST, PORT))

def receive():
    while True:
        try:
            message = client.recv(1024).decode('utf-8')

            if message == "NICK":
                client.send(nickname.encode('utf-8'))
            else:
                print(message, end="")  # clean formatting

        except:
            print("Disconnected from server.")
            client.close()
            break

def write():
    while True:
        msg = input()

        if msg.strip() == "/exit":
            client.send("/exit".encode('utf-8'))
            print("You left the chat.")
            client.close()
            break

        message = f"{nickname}: {msg}"
        client.send(message.encode('utf-8'))

receive_thread = threading.Thread(target=receive)
receive_thread.start()

write_thread = threading.Thread(target=write)
write_thread.start()