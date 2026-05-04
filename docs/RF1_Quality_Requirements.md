# RF1 — Requerimientos de Calidad y No Funcionales (PTT)

**Código:** RF1-QA  
**Módulo:** Control & Push-to-Talk  
**Versión:** 1.0  
**Rama:** `feature/rf1-ptt-hotkey`

---

## 1. Requerimientos No Funcionales

### RNF1.1 — Rendimiento (Latencia)

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF1.1 |
| **Categoría** | Rendimiento |

**Descripción:**  
El tiempo entre que el usuario presiona el hotkey y el sistema comienza a grabar audio debe ser menor a **50 ms**.

**Métrica:**
- Latencia de detección de tecla: < 50 ms (medido con logging de timestamps).
- Latencia de inicio de grabación: < 100 ms desde `on_press`.

---

### RNF1.2 — Robustez

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF1.2 |
| **Categoría** | Robustez |

**Descripción:**  
El listener de hotkeys no debe bloquear el hilo principal de la UI ni interferir con la reproducción de audio de pygame.

**Criterios:**
1. El listener corre como `daemon=True`.
2. Si `pynput` arroja una excepción (ej. permisos insuficientes en Windows), la app debe mostrar un mensaje de error en la consola pero **no cerrarse**.
3. La UI debe seguir respondiendo a 60 FPS mientras el listener está activo.

---

### RNF1.3 — Usabilidad

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF1.3 |
| **Categoría** | Usabilidad |

**Descripción:**  
El usuario debe entender el estado del sistema sin leer documentación.

**Criterios:**
1. El switch PTT muestra texto dinámico: "PTT: ON" / "PTT: OFF".
2. Cuando PTT está ON y se presiona el hotkey, un indicador visual (ej. círculo rojo) muestra "GRABANDO...".
3. Si el audio grabado es descartado por RMS bajo, se muestra un mensaje efímero en la consola: "[PTT] Audio descartado: silencio detectado".

---

### RNF1.4 — Compatibilidad

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF1.4 |
| **Categoría** | Compatibilidad |

**Descripción:**  
El sistema debe funcionar en Windows 10/11 con el entorno `flux_env` actual.

**Criterios:**
1. `pynput` utiliza la API nativa de Windows (`ctypes`) sin requerir privilegios de administrador.
2. Las teclas multimedia y botones de mouse extendidos (Mouse4, Mouse5) son detectables.
3. No debe interferir con atajos globales de otras aplicaciones (OBS, Discord, etc.).

---

## 2. Criterios de Aceptación (Definition of Done)

Para considerar RF1.1 y RF1.2 completos, se debe cumplir:

- [ ] El switch PTT aparece en la UI y cambia de modo en tiempo real.
- [ ] En modo PTT=ON, el WebSocket no procesa transcripciones automáticas.
- [ ] El dropdown de teclas permite seleccionar al menos 15 opciones predefinidas.
- [ ] Presionar y soltar el hotkey inicia y detiene la grabación correctamente.
- [ ] El audio grabado se envía al motor IA y genera una respuesta hablada.
- [ ] La latencia de detección del hotkey es < 50 ms en 95% de las pruebas.
- [ ] La aplicación no se bloquea ni crashea tras 100 ciclos de press/release.
- [ ] El modo PTT respeta la lógica half-duplex (no graba mientras habla).

---

## 3. Restricciones y Supuestos

1. **Entorno compartido:** No se debe degradar ni eliminar paquetes existentes en `flux_env`.
2. **Sin overlay:** La detección de teclas es por software (`pynput`), no requiere hook de bajo nivel ni drivers.
3. **Permisos:** Se asume que el usuario ejecuta VoiceAI con permisos normales (no admin requerido para teclas estándar).

