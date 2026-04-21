import base64
import binascii
import io
import json
import re
import socket
import threading
import uuid
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    from PIL import Image, UnidentifiedImageError
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    UnidentifiedImageError = OSError
    PIL_AVAILABLE = False

from database import (
    AUDIO_MESSAGE_TYPE,
    IMAGE_MESSAGE_TYPE,
    TEXT_MESSAGE_TYPE,
    get_last_messages,
    get_last_messages_with_ids,
    get_messages_after,
    init_db,
    save_message,
)
from rule_based_ai import RuleBasedAI

TCP_HOST = "0.0.0.0"
TCP_PORT = 12345
WEB_HOST = "0.0.0.0"
WEB_PORT = 8000
INDEX_FILE = Path(__file__).with_name("index.html")

MAX_NICKNAME_LEN = 24
MAX_MESSAGE_LEN = 500
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_UPLOAD_REQUEST_BYTES = 12 * 1024 * 1024
JPEG_QUALITY = 70
PROCESSED_IMAGE_FILE = Path(__file__).with_name("processed.jpg")
UPLOADS_DIR = Path(__file__).with_name("uploads")
SUPPORTED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}
IMAGE_DATA_URL_RE = re.compile(
    r"^data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/]+={0,2})$"
)
SUPPORTED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg"}
SUPPORTED_AUDIO_MIME_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/ogg",
    "application/ogg",
}
AUDIO_MIME_BY_EXTENSION = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
}

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((TCP_HOST, TCP_PORT))
server.listen()
server.settimeout(1)

running = True
clients = []
nicknames = []
clients_lock = threading.Lock()
web_server = None
chat_ai = RuleBasedAI()


def sanitize_text(value, max_length):
    clean = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    return clean[:max_length]


def parse_image_data_url(value):
    if not isinstance(value, str):
        return None

    clean = value.strip()
    match = IMAGE_DATA_URL_RE.fullmatch(clean)
    if not match:
        return None

    mime_type = match.group(1).lower()
    encoded_data = match.group(2)
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        return None

    try:
        raw_bytes = base64.b64decode(encoded_data, validate=True)
    except (binascii.Error, ValueError):
        return None

    if not raw_bytes or len(raw_bytes) > MAX_IMAGE_BYTES:
        return None

    return mime_type, raw_bytes


def sanitize_image_data_url(value):
    parsed = parse_image_data_url(value)
    if not parsed:
        return None

    mime_type, raw_bytes = parsed
    normalized = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:{mime_type};base64,{normalized}"


def compress_image_data_url(value):
    # Validate and decode the uploaded image.
    parsed = parse_image_data_url(value)
    if not parsed:
        return None

    _, raw_bytes = parsed
    original_size = len(raw_bytes)

    try:
        with Image.open(io.BytesIO(raw_bytes)) as image:
            if image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba_image = image.convert("RGBA")
                white_bg = Image.new("RGB", rgba_image.size, (255, 255, 255))
                white_bg.paste(rgba_image, mask=rgba_image.split()[-1])
                image_to_save = white_bg
            else:
                image_to_save = image.convert("RGB")

            output_buffer = io.BytesIO()
            image_to_save.save(
                output_buffer,
                format="JPEG",
                quality=JPEG_QUALITY,
                optimize=True,
            )
    except (UnidentifiedImageError, OSError, ValueError):
        return None

    processed_bytes = output_buffer.getvalue()
    if not processed_bytes:
        return None

    processed_size = len(processed_bytes)
    if processed_size > MAX_IMAGE_BYTES:
        return None

    reduction_percent = ((original_size - processed_size) / original_size) * 100
    reduction_percent = round(reduction_percent, 2)

    # Save the latest compressed image on disk for simple local comparison.
    PROCESSED_IMAGE_FILE.write_bytes(processed_bytes)

    processed_data = base64.b64encode(processed_bytes).decode("ascii")
    return {
        "imageData": f"data:image/jpeg;base64,{processed_data}",
        "originalBytes": original_size,
        "processedBytes": processed_size,
        "reductionPercent": reduction_percent,
    }


def build_audio_filename(original_name):
    extension = Path(original_name).suffix.lower()
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(original_name).stem).strip("_")
    if not safe_stem:
        safe_stem = "audio"
    safe_stem = safe_stem[:40]
    return f"{safe_stem}_{uuid.uuid4().hex[:12]}{extension}"


def is_valid_audio_upload(file_name, mime_type):
    extension = Path(file_name).suffix.lower()
    if extension not in SUPPORTED_AUDIO_EXTENSIONS:
        return False

    clean_mime = (mime_type or "").strip().lower()
    if clean_mime and clean_mime not in SUPPORTED_AUDIO_MIME_TYPES and not clean_mime.startswith("audio/"):
        return False

    return True


def broadcast_to_tcp(message):
    with clients_lock:
        active_clients = list(clients)

    failed_clients = []
    for client in active_clients:
        try:
            client.sendall(message)
        except OSError:
            failed_clients.append(client)

    for dead_client in failed_clients:
        remove_client(dead_client)


def remove_client(client):
    nickname = None

    with clients_lock:
        if client in clients:
            index = clients.index(client)
            nickname = nicknames[index]
            clients.pop(index)
            nicknames.pop(index)

    try:
        client.close()
    except OSError:
        pass

    if nickname:
        broadcast_to_tcp(f"{nickname} left the chat!\n".encode("utf-8"))


def store_and_broadcast(
    nickname,
    message,
    message_type=TEXT_MESSAGE_TYPE,
    image_data=None,
    audio_path=None,
    audio_size=None,
    image_stats=None,
):
    message_id = save_message(
        nickname,
        message,
        message_type,
        image_data,
        audio_path=audio_path,
        audio_size=audio_size,
        original_size=image_stats["originalBytes"] if image_stats else None,
        processed_size=image_stats["processedBytes"] if image_stats else None,
        reduction_percent=image_stats["reductionPercent"] if image_stats else None,
    )
    if message_type == IMAGE_MESSAGE_TYPE:
        tcp_text = "[Image]"
        if message:
            tcp_text = f"{tcp_text} {message}"
        broadcast_to_tcp(f"{nickname}: {tcp_text}\n".encode("utf-8"))
    elif message_type == AUDIO_MESSAGE_TYPE:
        tcp_text = "[Audio]"
        if message:
            tcp_text = f"{tcp_text} {message}"
        broadcast_to_tcp(f"{nickname}: {tcp_text}\n".encode("utf-8"))
    else:
        broadcast_to_tcp(f"{nickname}: {message}\n".encode("utf-8"))
    return message_id


def process_user_message(
    nickname,
    message,
    message_type=TEXT_MESSAGE_TYPE,
    image_data=None,
    audio_path=None,
    audio_size=None,
    image_stats=None,
):
    message_id = store_and_broadcast(
        nickname,
        message,
        message_type,
        image_data,
        audio_path=audio_path,
        audio_size=audio_size,
        image_stats=image_stats,
    )
    if message_type == TEXT_MESSAGE_TYPE:
        ai_reply = chat_ai.generate_reply(nickname, message)
        if ai_reply:
            store_and_broadcast(chat_ai.bot_name, ai_reply)
    return message_id


def handle_client(client):
    while running:
        try:
            data = client.recv(1024)
            if not data:
                raise ConnectionError("Disconnected")

            incoming = data.decode("utf-8", errors="ignore").strip()
            if not incoming:
                continue

            with clients_lock:
                if client not in clients:
                    break
                nickname = nicknames[clients.index(client)]

            if incoming == "/exit":
                remove_client(client)
                break

            clean_msg = incoming.split(": ", 1)[1] if ": " in incoming else incoming
            clean_msg = sanitize_text(clean_msg, MAX_MESSAGE_LEN)
            if not clean_msg:
                continue

            process_user_message(nickname, clean_msg, TEXT_MESSAGE_TYPE)

        except Exception:
            remove_client(client)
            break


class ChatHTTPRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=HTTPStatus.OK):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self):
        content_length = self.headers.get("Content-Length")
        if not content_length:
            return {}

        try:
            length = int(content_length)
        except ValueError:
            return None

        body = self.rfile.read(length)
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    def _read_body_bytes(self, max_bytes):
        content_length = self.headers.get("Content-Length")
        if not content_length:
            return None, "Missing Content-Length header"

        try:
            length = int(content_length)
        except ValueError:
            return None, "Invalid Content-Length header"

        if length < 0:
            return None, "Invalid Content-Length header"

        if length > max_bytes:
            return None, "Upload is too large"

        try:
            return self.rfile.read(length), None
        except OSError:
            return None, "Unable to read request body"

    def _parse_multipart_form_data(self, content_type, body_bytes):
        # Build a minimal MIME message so Python's email parser can split parts.
        header_block = (
            f"Content-Type: {content_type}\r\n"
            "MIME-Version: 1.0\r\n\r\n"
        ).encode("utf-8")

        try:
            message = BytesParser(policy=default).parsebytes(header_block + body_bytes)
        except Exception:
            return None, None

        if not message.is_multipart():
            return None, None

        fields = {}
        files = {}
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue

            field_name = part.get_param("name", header="content-disposition")
            if not field_name:
                continue

            payload = part.get_payload(decode=True) or b""
            filename = part.get_filename()

            if filename:
                files[field_name] = {
                    "filename": filename,
                    "content_type": part.get_content_type(),
                    "data": payload,
                }
            else:
                fields[field_name] = payload.decode("utf-8", errors="ignore")

        return fields, files

    def _serve_index(self):
        if not INDEX_FILE.exists():
            self._send_json(
                {"error": "index.html not found in project root"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        content = INDEX_FILE.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_uploaded_audio(self, request_path):
        # Only allow simple filenames under /uploads to avoid path traversal.
        encoded_name = request_path.replace("/uploads/", "", 1)
        decoded_name = unquote(encoded_name)
        filename = Path(decoded_name).name
        if not filename or filename != decoded_name:
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return

        file_path = UPLOADS_DIR / filename
        if not file_path.exists() or not file_path.is_file():
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return

        extension = file_path.suffix.lower()
        if extension not in SUPPORTED_AUDIO_EXTENSIONS:
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return

        try:
            content = file_path.read_bytes()
        except OSError:
            self._send_json({"error": "Unable to read audio file"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        mime_type = AUDIO_MIME_BY_EXTENSION.get(extension, "application/octet-stream")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _handle_audio_upload(self):
        content_type = self.headers.get("Content-Type", "")
        body_bytes, body_error = self._read_body_bytes(MAX_AUDIO_UPLOAD_REQUEST_BYTES)
        if body_error:
            if body_error == "Upload is too large":
                self._send_json(
                    {"error": "Audio upload request is too large"},
                    status=HTTPStatus.BAD_REQUEST,
                )
            else:
                self._send_json({"error": body_error}, status=HTTPStatus.BAD_REQUEST)
            return

        fields, files = self._parse_multipart_form_data(content_type, body_bytes)
        if fields is None or files is None:
            self._send_json({"error": "Invalid form data"}, status=HTTPStatus.BAD_REQUEST)
            return

        nickname = sanitize_text(str(fields.get("nickname", "")), MAX_NICKNAME_LEN)
        message_type = sanitize_text(
            str(fields.get("type", AUDIO_MESSAGE_TYPE)).lower(), 10
        )
        message = sanitize_text(str(fields.get("message", "")), MAX_MESSAGE_LEN)

        if not nickname:
            self._send_json(
                {"error": "Nickname is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        if message_type != AUDIO_MESSAGE_TYPE:
            self._send_json(
                {"error": "Audio upload must use type=audio"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        if "audioFile" not in files:
            self._send_json(
                {"error": "No audio file uploaded"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        file_info = files["audioFile"]
        file_name = str(file_info.get("filename", ""))
        mime_type = str(file_info.get("content_type", "")).lower()
        audio_bytes = file_info.get("data", b"")

        if not file_name:
            self._send_json(
                {"error": "No audio file uploaded"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        if not is_valid_audio_upload(file_name, mime_type):
            self._send_json(
                {"error": "Audio must be MP3, WAV, or OGG"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        if not audio_bytes:
            self._send_json(
                {"error": "Audio file is empty"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        if len(audio_bytes) > MAX_AUDIO_BYTES:
            self._send_json(
                {"error": "Audio is too large. Max size is 10 MB."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        UPLOADS_DIR.mkdir(exist_ok=True)
        stored_name = build_audio_filename(file_name)
        stored_path = UPLOADS_DIR / stored_name

        try:
            stored_path.write_bytes(audio_bytes)
        except OSError:
            self._send_json(
                {"error": "Unable to save audio file"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        audio_path = f"/uploads/{stored_name}"
        audio_size = len(audio_bytes)
        message_id = process_user_message(
            nickname,
            message,
            message_type=AUDIO_MESSAGE_TYPE,
            audio_path=audio_path,
            audio_size=audio_size,
        )
        self._send_json(
            {
                "id": message_id,
                "nickname": nickname,
                "message": message,
                "type": AUDIO_MESSAGE_TYPE,
                "imageData": None,
                "imageStats": None,
                "audioPath": audio_path,
                "audioSize": audio_size,
            },
            status=HTTPStatus.CREATED,
        )

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            self._serve_index()
            return

        if parsed.path.startswith("/uploads/"):
            self._serve_uploaded_audio(parsed.path)
            return

        if parsed.path == "/api/health":
            self._send_json({"status": "ok"})
            return

        if parsed.path == "/api/ai":
            self._send_json({"botName": chat_ai.bot_name, "mode": "rule-based"})
            return

        if parsed.path == "/api/messages":
            params = parse_qs(parsed.query)
            after_raw = params.get("after", ["0"])[0]
            limit_raw = params.get("limit", ["50"])[0]

            try:
                after = max(0, int(after_raw))
                limit = int(limit_raw)
            except ValueError:
                self._send_json(
                    {"error": "Invalid query params. Use integers for after/limit."},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return

            limit = max(1, min(limit, 200))
            if after > 0:
                rows = get_messages_after(after, limit)
            else:
                rows = get_last_messages_with_ids(limit)

            messages = []
            for (
                msg_id,
                nickname,
                message,
                message_type,
                image_data,
                audio_path,
                audio_size,
                original_size,
                processed_size,
                reduction_percent,
            ) in rows:
                normalized_type = (
                    message_type
                    if message_type in (
                        TEXT_MESSAGE_TYPE,
                        IMAGE_MESSAGE_TYPE,
                        AUDIO_MESSAGE_TYPE,
                    )
                    else TEXT_MESSAGE_TYPE
                )

                image_stats = None
                if (
                    normalized_type == IMAGE_MESSAGE_TYPE
                    and isinstance(original_size, int)
                    and isinstance(processed_size, int)
                    and reduction_percent is not None
                ):
                    image_stats = {
                        "originalBytes": original_size,
                        "processedBytes": processed_size,
                        "reductionPercent": round(float(reduction_percent), 2),
                    }

                messages.append(
                    {
                        "id": msg_id,
                        "nickname": nickname,
                        "message": message,
                        "type": normalized_type,
                        "imageData": (
                            image_data if normalized_type == IMAGE_MESSAGE_TYPE else None
                        ),
                        "imageStats": image_stats if normalized_type == IMAGE_MESSAGE_TYPE else None,
                        "audioPath": audio_path if normalized_type == AUDIO_MESSAGE_TYPE else None,
                        "audioSize": audio_size if normalized_type == AUDIO_MESSAGE_TYPE else None,
                    }
                )
            self._send_json({"messages": messages})
            return

        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/messages":
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return

        content_type = (self.headers.get("Content-Type", "") or "").lower()
        if content_type.startswith("multipart/form-data"):
            self._handle_audio_upload()
            return

        payload = self._read_json()
        if payload is None:
            self._send_json({"error": "Invalid JSON body"}, status=HTTPStatus.BAD_REQUEST)
            return

        nickname = sanitize_text(str(payload.get("nickname", "")), MAX_NICKNAME_LEN)
        message_type = sanitize_text(
            str(payload.get("type", TEXT_MESSAGE_TYPE)).lower(), 10
        )
        if message_type not in (TEXT_MESSAGE_TYPE, IMAGE_MESSAGE_TYPE):
            self._send_json(
                {"error": "Message type must be text or image (audio uses file upload)"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        message = sanitize_text(str(payload.get("message", "")), MAX_MESSAGE_LEN)
        image_data = None
        image_stats = None

        if not nickname:
            self._send_json(
                {"error": "Nickname is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        if message_type == TEXT_MESSAGE_TYPE:
            if not message:
                self._send_json(
                    {"error": "Message is required"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
        else:
            image_data = sanitize_image_data_url(payload.get("imageData", ""))
            if not image_data:
                self._send_json(
                    {
                        "error": (
                            "A valid image is required (jpg/png/gif/webp, max 2 MB)"
                        )
                    },
                    status=HTTPStatus.BAD_REQUEST,
                )
                return

            if not PIL_AVAILABLE:
                self._send_json(
                    {
                        "error": (
                            "Image compression requires Pillow. "
                            "Install it with: pip install pillow"
                        )
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

            compression_result = compress_image_data_url(image_data)
            if not compression_result:
                self._send_json(
                    {"error": "Image could not be processed. Please choose another image."},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return

            image_data = compression_result["imageData"]
            image_stats = {
                "originalBytes": compression_result["originalBytes"],
                "processedBytes": compression_result["processedBytes"],
                "reductionPercent": compression_result["reductionPercent"],
            }
            print(
                "Image compressed:",
                f"original={image_stats['originalBytes']} bytes,",
                f"processed={image_stats['processedBytes']} bytes,",
                f"reduction={image_stats['reductionPercent']}%",
            )

        message_id = process_user_message(
            nickname,
            message,
            message_type=message_type,
            image_data=image_data,
            image_stats=image_stats,
        )
        self._send_json(
            {
                "id": message_id,
                "nickname": nickname,
                "message": message,
                "type": message_type,
                "imageData": image_data if message_type == IMAGE_MESSAGE_TYPE else None,
                "imageStats": image_stats if message_type == IMAGE_MESSAGE_TYPE else None,
                "audioPath": None,
                "audioSize": None,
            },
            status=HTTPStatus.CREATED,
        )

    def log_message(self, format_string, *args):
        return


def get_local_ip():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        local_ip = probe.getsockname()[0]
    except OSError:
        local_ip = "127.0.0.1"
    finally:
        probe.close()
    return local_ip


def receive():
    global running
    print(f"TCP server running on {TCP_HOST}:{TCP_PORT}")

    try:
        while running:
            try:
                client, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            print(f"Connected with {address}")
            client.sendall("NICK".encode("utf-8"))
            nickname_data = client.recv(1024)
            nickname = sanitize_text(
                nickname_data.decode("utf-8", errors="ignore"), MAX_NICKNAME_LEN
            )
            if not nickname:
                nickname = f"Guest-{address[1]}"

            with clients_lock:
                nicknames.append(nickname)
                clients.append(client)

            print(f"Nickname is {nickname}")

            history = get_last_messages()
            for nick, msg in history:
                client.sendall(f"{nick}: {msg}\n".encode("utf-8"))

            broadcast_to_tcp(f"{nickname} joined the chat!\n".encode("utf-8"))
            client.sendall("Connected to server!\n".encode("utf-8"))

            thread = threading.Thread(target=handle_client, args=(client,), daemon=True)
            thread.start()

    except KeyboardInterrupt:
        pass
    finally:
        shutdown()


def shutdown():
    global running
    global web_server

    if not running:
        return

    running = False
    print("\nShutting down server...")

    if web_server is not None:
        web_server.shutdown()
        web_server.server_close()

    with clients_lock:
        active_clients = list(clients)
        clients.clear()
        nicknames.clear()

    for client in active_clients:
        try:
            client.sendall("Server shutting down...\n".encode("utf-8"))
            client.close()
        except OSError:
            pass

    try:
        server.close()
    except OSError:
        pass

    print("Server closed successfully.")


def start_web_server():
    global web_server

    web_server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), ChatHTTPRequestHandler)
    thread = threading.Thread(target=web_server.serve_forever, daemon=True)
    thread.start()
    return thread


def main():
    init_db()
    start_web_server()

    local_ip = get_local_ip()
    print(f"Web chat available at: http://{local_ip}:{WEB_PORT}")
    print(f"Local browser link: http://127.0.0.1:{WEB_PORT}")
    print(f"Rule AI bot active as: {chat_ai.bot_name} (use @localai help)")
    print("Press Ctrl+C to stop.\n")

    receive()


if __name__ == "__main__":
    main()
