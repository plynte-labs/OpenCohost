# ADR-007: UI Performance Bottlenecks — Bounded Queue Processing, Batched Text Updates, and Task Throttling

**Date**: 2026-06-09  
**Status**: Implemented (2026-06-10)  
**Branch**: `audit/ui-rendering-analysis`  
**Author**: Antigravity & UI Performance Auditor Subagent  
**Scope**: `ui/advanced_panel.py` and `ui/app_shell.py` performance optimization, log queue handling, dialogue textbox updates, and event queue scheduling.

---

## Context

OpenCohost is a live-streaming companion app. The user interface must display high-frequency events (e.g., token-by-token text streaming from Kira, RMS audio levels, system telemetry, and YouTube/Twitch chat ingestion) in real-time. Because CustomTkinter runs on a single-threaded event loop, high-frequency updates easily saturate the main loop, causing UI freezes, layout flickering, and high CPU usage.

This document details the performance QA audit findings, justifies why fixing these bottlenecks is critical for live-streaming environments, and outlines the required architectural corrections.

---

## Findings & Code Audit

### 1. Unbounded Queue Consumption in `process_logs`
*   **Location**: `ui/advanced_panel.py` (lines 297-308)
*   **The Code**:
    ```python
    def process_logs(self) -> None:
        while True:
            try:
                msg = self._log_queue.get_nowait()
                self.print_log(msg)
            except queue.Empty:
                break
    ```
*   **The Issue**: The loop attempts to drain the log queue completely in a single GUI tick. If a large burst of logs is generated (e.g., websocket connectivity retries, chat flood, status reports), this loop blocks the main thread for the duration of the entire read-and-render sequence.

### 2. Layout Thrashing in `append_to_textbox`
*   **Location**: `ui/advanced_panel.py` (lines 272-291)
*   **The Code**:
    ```python
    def append_to_textbox(self, widget: ctk.CTkTextbox, line: Any, max_lines: int = 1000) -> None:
        safe_line = str(line).replace("\r", " ").replace("\n", " ")
        widget.configure(state="normal")
        widget.insert("end", safe_line + "\n")
        try:
            total_lines = int(widget.index("end-1c").split(".")[0])
            excess = total_lines - int(max_lines)
            if excess > 0:
                widget.delete("1.0", f"{excess + 1}.0")
        except Exception:
            pass
        widget.see("end")
        widget.configure(state="disabled")
    ```
*   **The Issue**: Every log line triggers multiple heavy operations. In particular, `widget.index("end-1c")` and `widget.see("end")` force the Tkinter layout engine to synchronously recalculate the entire document layout to locate character lines and viewport offsets. Running this layout calculation on every line in a loop causes severe **layout thrashing** and drives high CPU usage.

### 3. Redundant Reflows in `update_kira_response`
*   **Location**: `ui/advanced_panel.py` (lines 393-410)
*   **The Issue**: For every log string containing `[Kira]:`, the entire textbox is cleared and replaced (`delete("1.0", "end")` followed by `insert(...)`). During token-by-token streaming, this causes constant layout reflows, causing the text area to flicker and stutter.

### 4. Event Queue Flooding in `_process_ui_tasks`
*   **Location**: `ui/app_shell.py` (lines 2268-2288)
*   **The Issue**: The UI task consumer reads the task queue and immediately registers *every* callback using `self.after(delay_ms, func)`. High-frequency telemetry (e.g., 60Hz audio meter frames) registers hundreds of separate callbacks in the Tcl/Tk event queue, leading to high CPU core usage.

---

## Why Fixing This is Critical for Streamers

1.  **OBS & Encoder Preservation**: Live encoders (like OBS Studio) and modern video games require maximum CPU/GPU stability. An unbounded UI loop that thrashing the CPU cores causes micro-stutters in games and dropped frames in the video stream.
2.  **Audio Crackling Prevention**: Stalling the CPU main thread interferes with real-time audio thread scheduling (e.g., `sounddevice` / PortAudio callbacks), leading to pops, clicks, or latency in the streamer's microphone audio.
3.  **Operator Control Safety**: If the interface freezes, the streamer or operator cannot toggle critical features, change models, or view warning notifications.

---

## Implementation Notes (2026-06-10)

- **Fix 1 (chunk-limited processing + batched writes)**: Implemented in
  `ui/advanced_panel.py` — `process_logs` now processes at most
  `PROCESS_LOGS_CHUNK_LIMIT` (20) messages per tick, flushes the console via
  the new `append_batch_to_textbox`, and reschedules a continuation through
  `schedule_ui_update` when the queue still has items. `append_to_textbox`
  delegates to the batch variant.
- **Fix 2 (equality guard)**: Implemented in `update_kira_response` — identical
  content returns early before the delete/insert cycle.
- **Fix 3 (`_process_ui_tasks`)**: Audit found the current implementation in
  `ui/app_shell.py` already drains the queue and re-schedules with
  `after(50)`; no change was required.
- **Tests**: 4 new cases in `tests/test_advanced_panel.py` cover chunk
  bounding, continuation rescheduling, single batched console write, and the
  identical-content guard.

## Proposed Refactored Implementations

### Fix 1: Chunk-Limited Log Processing & Batched Text Box Writes
Replace the blocking `while True` loop with a bounded processing chunk. Accumulate multiple log lines in an in-memory list, and write them in a single batch, performing layout queries (`index`) and viewport shifts (`see`) **once per batch**.

```python
    def process_logs(self) -> None:
        """Process pending log messages with chunk-limiting to prevent thread starvation."""
        batch_messages = []
        chunk_limit = 20  # Max logs processed in a single tick
        processed_count = 0
        
        while processed_count < chunk_limit:
            try:
                msg = self._log_queue.get_nowait()
                msg_str = str(msg)
                
                # Check/update Kira response display
                self.update_kira_response(msg_str)
                
                if self._logs_panel_visible and self.consola is not None:
                    batch_messages.append(msg_str)
                
                processed_count += 1
            except queue.Empty:
                break
                
        # Flush the accumulated messages in a single update
        if batch_messages:
            self.append_batch_to_textbox(self.consola, batch_messages, max_lines=1500)
            
        # Yield to event loop, then immediately process the rest of the queue if not empty
        if not self._log_queue.empty():
            self._schedule_ui_update(lambda: self.process_logs())

    def append_batch_to_textbox(self, widget: ctk.CTkTextbox, lines: list[str], max_lines: int = 1000) -> None:
        """Appends a batch of lines to the textbox, minimizing layout updates and layout thrashing."""
        if not lines:
            return
            
        # Clean and join all lines in the batch
        batch_text = "\n".join(str(line).replace("\r", " ").replace("\n", " ") for line in lines) + "\n"
        
        widget.configure(state="normal")
        widget.insert("end", batch_text)
        
        try:
            # Query index ONLY ONCE for the entire batch
            total_lines = int(widget.index("end-1c").split(".")[0])
            excess = total_lines - int(max_lines)
            if excess > 0:
                widget.delete("1.0", f"{excess + 1}.0")
        except Exception:
            pass
            
        # Perform layout-driven draw calls once
        widget.see("end")
        widget.configure(state="disabled")
```

### Fix 2: Equality Guards for Kira Responses
Prevent layout thrashing during text updates by guarding updates with a string equality check.

```python
    def update_kira_response(self, msg: str) -> None:
        """Update the Kira response panel safely, preventing redundant rendering cycles."""
        if self.text_kira_response is None:
            return
        if "[Kira]:" not in msg:
            return

        response = msg.strip()
        response = response.replace("🧠 ", "")
        
        # Guard: check if current content is identical to avoid redundant layout calculations
        try:
            current_text = self.text_kira_response.get("1.0", "end-1c").strip()
            if current_text == response:
                return
        except Exception:
            pass

        self.text_kira_response.configure(state="normal")
        self.text_kira_response.delete("1.0", "end")
        self.text_kira_response.insert("end", response + "\n")
        self.text_kira_response.configure(state="disabled")
```

### Fix 3: UI Task Dequeueing Optimization
Gather pending UI callbacks and optimize their scheduling on the event loop.

```python
    def _process_ui_tasks(self) -> None:
        """Process pending UI tasks with batch processing and scheduled throttle guards."""
        task_queue = self.__dict__.get("_ui_task_queue")
        if task_queue is None:
            return

        # Dequeue all tasks in memory
        tasks = []
        while True:
            try:
                tasks.append(task_queue.get_nowait())
            except queue.Empty:
                break
                
        # Register and process callbacks
        for delay_ms, func in tasks:
            try:
                self.after(delay_ms, func)
            except RuntimeError:
                pass

        if not self.__dict__.get("_closing", False):
            try:
                self.after(50, self._process_ui_tasks)
            except RuntimeError:
                pass
```
