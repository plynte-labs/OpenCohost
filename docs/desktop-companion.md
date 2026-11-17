# Desktop companion

Kira can run as a small companion window on your desktop while the main OpenCohost
window is minimized.

## What it is

- A small, frameless window with Kira. It is hidden from the taskbar.
- It shows only when all three are true: desktop mode is on, the main window is
  minimized, and Kira is ready.
- It hides again as soon as you restore the main window.

## Over other apps

- Intended behavior (in progress): the companion stays above other windows without
  taking focus, so the app you are typing in keeps the keyboard.

## Exclusive fullscreen games

- Games in exclusive fullscreen (DirectX or Vulkan) draw over every window. No normal
  app can appear above them, and the companion is no exception.
- OpenCohost does not inject into game processes.
- Recommendation: run the game in **Borderless windowed** (also called
  **Fullscreen windowed**) mode. The companion can then stay visible above the game.

## Getting the main window back

- Restore the main window from the taskbar. The companion hides and the full app is
  back.

## Closing

- Closing the main window closes the app. The companion closes with it.
- Closing the companion window turns desktop mode off and restores the main window.
