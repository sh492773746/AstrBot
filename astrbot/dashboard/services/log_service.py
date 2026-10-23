from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

from astrbot.core import LogBroker, LogManager, logger
from astrbot.core.config.astrbot_config import AstrBotConfig


class LogServiceError(Exception):
    pass


class LogService:
    def __init__(self, log_broker: LogBroker, config: AstrBotConfig) -> None:
        self.log_broker = log_broker
        self.config = config

    @staticmethod
    def format_log_sse(log: dict, ts: float) -> str:
        payload = {
            "type": "log",
            **log,
        }
        return f"id: {ts}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def replay_cached_logs(self, last_event_id: str) -> AsyncGenerator[str, None]:
        try:
            last_ts = float(last_event_id)
            cached_logs = list(self.log_broker.log_cache)

            for log_item in cached_logs:
                log_ts = float(log_item.get("time", 0))
                if log_ts > last_ts:
                    yield self.format_log_sse(log_item, log_ts)
        except ValueError:
            pass
        except Exception as exc:
            logger.error(f"Log SSE 补发历史错误: {exc}")

    async def stream_log_events(
        self, last_event_id: str | None
    ) -> AsyncGenerator[str, None]:
        queue = None
        try:
            queue = self.log_broker.register()
            if last_event_id:
                async for event in self.replay_cached_logs(last_event_id):
                    yield event

            while True:
                message = await queue.get()
                current_ts = message.get("time", time.time())
                yield self.format_log_sse(message, current_ts)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Log SSE 连接错误: {exc}")
        finally:
            if queue:
                self.log_broker.unregister(queue)

    def get_log_history(self) -> dict:
        try:
            cached = list(self.log_broker.log_cache)
            archived = []
            for trace in (False, True):
                prefix = "trace_log" if trace else "log_file"
                if not self.config.get(f"{prefix}_enable", False):
                    continue
                path = Path(
                    LogManager._resolve_log_path(
                        self.config.get(f"{prefix}_path")
                        or ("logs/astrbot.trace.log" if trace else "logs/astrbot.log")
                    )
                )
                if not path.is_file():
                    continue
                earliest = min(
                    (
                        float(row.get("time", 0))
                        for row in cached
                        if (row.get("type") == "trace") == trace
                    ),
                    default=float("inf"),
                )
                # Read a bounded tail; archived files remain the complete source.
                with path.open("rb") as stream:
                    size = stream.seek(0, 2)
                    stream.seek(max(0, size - 4 * 1024 * 1024))
                    if stream.tell():
                        stream.readline()
                    lines = stream.read().decode("utf-8", errors="replace").splitlines()
                entries = []
                for line in lines:
                    try:
                        timestamp = datetime.strptime(
                            line[1:24], "%Y-%m-%d %H:%M:%S.%f"
                        ).timestamp()
                        if trace:
                            entry = json.loads(line[26:])
                            if (
                                not isinstance(entry, dict)
                                or entry.get("type") != "trace"
                            ):
                                continue
                        else:
                            entry = {
                                "type": "log",
                                "time": timestamp,
                                "level": "INFO",
                                "data": line,
                                "category": (
                                    match.group(1)
                                    if (
                                        match := re.search(
                                            r"\[category=([a-z_]+)\]:", line
                                        )
                                    )
                                    else "unknown"
                                ),
                            }
                            for level in (
                                "DEBUG",
                                "INFO",
                                "WARNING",
                                "WARN",
                                "ERROR",
                                "CRITICAL",
                            ):
                                if f"[{level}]" in line:
                                    entry["level"] = level
                                    break
                        entries.append(entry)
                    except (ValueError, TypeError):
                        if not trace and entries:
                            entries[-1]["data"] += "\n" + line
                archived.extend(
                    entry
                    for entry in entries[-1000:]
                    if float(entry.get("time", 0)) < earliest
                )
            logs = sorted(
                [*archived, *cached], key=lambda entry: float(entry.get("time", 0))
            )
            return {"logs": logs[-2000:]}
        except Exception as exc:
            logger.error(f"获取日志历史失败: {exc}")
            raise LogServiceError(f"获取日志历史失败: {exc}") from exc

    def get_trace_settings(self) -> dict:
        try:
            return {"trace_enable": self.config.get("trace_enable", True)}
        except Exception as exc:
            logger.error(f"获取 Trace 设置失败: {exc}")
            raise LogServiceError(f"获取 Trace 设置失败: {exc}") from exc

    def update_trace_settings(self, payload: dict | None) -> str:
        try:
            if payload is None:
                raise LogServiceError("请求数据为空")

            trace_enable = payload.get("trace_enable")
            if trace_enable is not None:
                self.config["trace_enable"] = bool(trace_enable)
                self.config.save_config()

            return "Trace 设置已更新"
        except LogServiceError:
            raise
        except Exception as exc:
            logger.error(f"更新 Trace 设置失败: {exc}")
            raise LogServiceError(f"更新 Trace 设置失败: {exc}") from exc
