import io
import wave

import httpx


def wav_bytes(seconds: float = 0.1, rate: int = 16000, extra_chunk: bool = False) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(seconds * rate))
    data = buf.getvalue()
    if extra_chunk:   # a LIST chunk before "data", like many encoders write
        riff, fmt, rest = data[:12], data[12:36], data[36:]
        chunk = b"LIST" + (4).to_bytes(4, "little") + b"INFO"
        body = riff[:4] + (len(data) - 8 + len(chunk)).to_bytes(4, "little") + riff[8:] + fmt + chunk + rest
        data = body
    return data


def mock_client(handler) -> tuple[httpx.Client, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped)), seen
