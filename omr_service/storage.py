"""Where jobs live: blobs (files by name) and a queue of messages, in Azure Storage when STORAGE_ACCOUNT is
set, otherwise in a local folder with the same behaviour (tests and local runs)."""

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Message:
    id: str
    text: str
    dequeue_count: int  # 1 on the first delivery
    receipt: object  # what the backend needs to extend or delete it


class AzureStore:
    """Signs in with DefaultAzureCredential: the managed identity in Azure (AZURE_CLIENT_ID), `az login` locally."""

    def __init__(self, settings):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
        from azure.storage.queue import QueueClient

        credential = DefaultAzureCredential()
        account = settings.storage_account
        self._blobs = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential) \
            .get_container_client(settings.blob_container)
        self._queue = QueueClient(f"https://{account}.queue.core.windows.net", settings.queue_name,
                                  credential=credential)

    def put(self, name, data, content_type="application/octet-stream"):
        from azure.storage.blob import ContentSettings
        self._blobs.upload_blob(name, data, overwrite=True, content_settings=ContentSettings(content_type=content_type))

    def get(self, name):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            return self._blobs.download_blob(name).readall()
        except ResourceNotFoundError:
            return None

    def send(self, text):
        self._queue.send_message(text)

    def receive(self, visibility_s):
        for msg in self._queue.receive_messages(max_messages=1, visibility_timeout=visibility_s):
            return Message(msg.id, msg.content, msg.dequeue_count, msg)
        return None

    def extend(self, message, visibility_s):
        updated = self._queue.update_message(message.receipt, visibility_timeout=visibility_s)
        message.receipt.pop_receipt = updated.pop_receipt

    def delete(self, message):
        self._queue.delete_message(message.receipt)

    def queue_length(self):
        return self._queue.get_queue_properties().approximate_message_count


class LocalStore:
    """Blobs are files under blobs/. Each message is a file under queue/ named <id>__<deliveries>__<visible at>;
    a receiver claims it by renaming it, which only one process can do."""

    def __init__(self, root):
        self._blobs = Path(root) / "blobs"
        self._queue = Path(root) / "queue"
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._queue.mkdir(parents=True, exist_ok=True)

    def put(self, name, data, content_type=None):
        path = self._blobs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, name):
        path = self._blobs / name
        return path.read_bytes() if path.is_file() else None

    def send(self, text):
        (self._queue / f"{uuid.uuid4().hex}__0__0").write_text(text)

    def receive(self, visibility_s):
        now = time.time()
        for path in sorted(self._queue.iterdir(), key=os.path.getmtime):
            msg_id, count, visible_at = path.name.split("__")
            if float(visible_at) > now:
                continue
            claimed = self._queue / f"{msg_id}__{int(count) + 1}__{now + visibility_s}"
            try:
                path.rename(claimed)
            except FileNotFoundError:
                continue  # another worker took it
            return Message(msg_id, claimed.read_text(), int(count) + 1, claimed)
        return None

    def extend(self, message, visibility_s):
        renewed = self._queue / f"{message.id}__{message.dequeue_count}__{time.time() + visibility_s}"
        message.receipt.rename(renewed)
        message.receipt = renewed

    def delete(self, message):
        message.receipt.unlink(missing_ok=True)

    def queue_length(self):
        return sum(1 for _ in self._queue.iterdir())


def make_store(settings):
    return AzureStore(settings) if settings.storage_account else LocalStore(settings.store_dir)
