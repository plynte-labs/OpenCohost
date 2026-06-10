# ADR-006: UI Responsiveness, Antialiasing, and VSync Rendering Analysis

**Date**: 2026-06-09  
**Status**: Accepted (2026-06-10) — Patterns 2 and 3 partially implemented via ADR-007  
**Branch**: `audit/ui-rendering-analysis`  
**Author**: Antigravity (Senior UI Architect)  
**Scope**: CustomTkinter UI performance, High DPI scaling, scrolling quality, and layout stability during dialogue streaming.

---

## Context

OpenCohost is a desktop application utilizing **CustomTkinter** (which builds on Tkinter and the standard Tk window system) for its graphical user interface. As the product expands, the UI must handle high-frequency events: RMS voice levels, stream status logs, OBS websocket heartbeats, and dialogue generation streaming.

Under load, operators report **UI lag, flickering, screen freezes, scrolling blurriness, and pixelated text**. 

This document audits the underlying Tk rendering pipeline, explains why these issues occur, and details the architectural patterns required to resolve them.

---

## Technical Audit & Root Causes

### 1. VSync (Vertical Synchronization) & Screen Tearing
*   **The Issue**: Tkinter has no native integration with modern GPU swapchains (DirectX, OpenGL, or Vulkan). On Windows, it relies on standard Win32 **GDI/GDI+ (Graphics Device Interface)** for software-based rasterization.
*   **Root Cause**: The Tk event loop (`update()`, `update_idletasks()`) runs redraw cycles on the main thread without aligning with the monitor's vertical blanking interval (VBlank). 
*   **Result**: When scrolling or resizing, GDI writes pixel buffers directly to the screen mid-refresh, causing visible horizontal screen tearing. 
*   **DWM Compositing Lag**: While Windows Desktop Window Manager (DWM) uses GPU-backed VSync to composite windows, it must capture GDI software-redirection surfaces. If the CPU main thread stalls during heavy layout calculations, DWM displays cached frames, causing stutter and frame drops.

### 2. Antialiasing & High DPI Scaling Distortion
*   **Antialiasing Limits**: Tkinter’s `Canvas` (the foundation of CustomTkinter’s rounded corners, shadows, and borders) uses integer coordinates and software rasterization. GDI drawing commands lack subpixel vector antialiasing. Diagonal lines and arcs appear jagged and pixelated.
*   **High DPI Blurriness**: If the OS DPI scale is high (e.g., 150% or 200% on a 4K monitor), two failure modes occur:
    1.  **System Scaling (Not DPI-Aware)**: The OS stretches the 96 DPI GDI window bitmap using bilinear filtering, making the entire UI look fuzzy and blurred.
    2.  **Per-Monitor DPI-Aware (Current)**: CustomTkinter manually adjusts fonts and geometry. However, rounding fractional coordinates to integer pixels (e.g., at 1.5x scale) causes subpixel misalignments, rendering borders and fonts pixelated or asymmetric.

### 3. Layout Thrashing & Main Thread Blocking (Kira Responses)
*   **Inference Ingestion**: When Kira’s response is updated, `AdvancedModePanel.print_log` (triggered by log queue inputs containing `[Kira]:`) modifies the `self.text_kira_response` (`CTkTextbox`) widget.
*   **Layout Thrashing**: Any text modification in Tkinter triggers **geometry propagation** up and down the widget tree (`grid_propagate`/`pack_propagate`). The single-threaded GUI thread must recalculate the bounds of all parent and sibling widgets, causing rendering freezes.
*   **Queue Congestion**: The log consumer loop in `AdvancedModePanel.process_logs` runs every 100ms:
    ```python
    while True:
        try:
            msg = self._log_queue.get_nowait()
            self.print_log(msg)  # Updates textboxes, calls see("end"), and recalculates indices
        except queue.Empty:
            break
    ```
    If a burst of logs arrives (e.g., RMS levels, websocket packets, or LLM tokens), the `while True` loop processes hundreds of items in a single GUI tick, completely locking the main thread.

### 4. Scrolling Blurriness & Redraw Overhead
*   **Overhead**: A `CTkScrollableFrame` wraps a standard Tk `Canvas`. When scrolling, CustomTkinter shifts the viewport coordinate.
*   **Redraw Overhead**: Shifted elements must be redrawn. Because CustomTkinter widgets are composite objects (a `CTkFrame` contains a canvas and a frame), Tk must execute many software drawing commands.
*   **DWM Texture Stretching**: Under high CPU rendering load, Tk drops frames. DWM attempts to maintain smooth window composition by stretching a cached bitmap of the window during motion. This causes a visible **blurry or pixelated smear** until the CPU thread catches up and redraws the widgets at their final scroll coordinates.

---

## Proposed Architectural Fixes (The 3 Patterns + Specifics)

To address these rendering bottlenecks, we must enforce three core patterns in the UI module:

### Pattern 1: Thread-Safe Background Offloading
*   **Rule**: Never run disk/network I/O, heavy parsing, or model switching directly on the main thread.
*   **Mechanism**: Wrap tasks in `threading.Thread(daemon=True)` and queue UI updates using `_safe_after(callback)`.

### Pattern 2: Debounced Progress & Log Flushing
*   **Rule**: Cap high-frequency UI updates. Do not modify widgets on every incoming queue item.
*   **Implementation**:
    1.  **Debounce Buffer**: Accumulate incoming log entries or token streams in an in-memory buffer.
    2.  **Capped Refresher**: Flush the buffer to the textbox at a capped rate (e.g., every 75ms) using a single widget update.
    3.  **Bounded Log Processing**: Replace the blocking `while True` loop in `process_logs` with a chunk-based processor (e.g., max 15 items per tick), scheduling remaining logs for the next tick using `after(10)`.

### Pattern 3: Incremental Batch Rendering (Lazy Loading)
*   **Rule**: Never render hundreds of complex widgets (such as logs, cards, or config profiles) in a single loop iteration.
*   **Implementation**:
    1.  Divide items into small batches (e.g., `BATCH_SIZE = 20`).
    2.  Render the first batch, then schedule the next via `self.after(10, self._render_batch)` to yield execution back to the event loop.

---

## Detailed Track Proposal: UI Rendering Optimization

We will establish a new Conductor track `ui_rendering_optimization_20260609` to implement these fixes.

### Track Specification Summary
*   **Objective**: Eliminate UI lag, scrolling blurriness, and frame freezes.
*   **Scope**: `ui/app_shell.py`, `ui/advanced_panel.py`, and `ui/stream_admin_ui.py`.
*   **Key Tasks**:
    1.  Introduce debounced log updates in `AdvancedModePanel`.
    2.  Implement a chunk-limited queue reader in `process_logs`.
    3.  Apply batch rendering to log frames, profiles, and stream status tables.
    4.  Verify performance using simulated high-frequency log updates.
