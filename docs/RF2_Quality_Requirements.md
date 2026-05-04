# RF2 — Requerimientos de Calidad y No Funcionales (UI/UX)

**Código:** RF2-QA  
**Módulo:** UI/UX y Pipeline Visual  
**Versión:** 1.0  
**Rama:** `feature/rf2-ui-ux`

---

## 1. Requerimientos No Funcionales

### RNF2.1 — Rendimiento (0% GPU overhead)

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF2.1 |
| **Categoría** | Rendimiento |

**Descripción:**  
La UI no debe consumir GPU significativa. CustomTkinter ya cumple esto por ser nativo. El requisito es no introducir animaciones, overlays, ni renderizados que usen ciclos de GPU.

**Métrica:**
- Uso de GPU de la UI: < 1% en RTX 3060.
- Sin dependencias nuevas de renderizado (no OpenGL, no DirectX).

---

### RNF2.2 — Usabilidad (Claridad visual)

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF2.2 |
| **Categoría** | Usabilidad |

**Descripción:**  
Los indicadores de pipeline deben ser legibles de un vistazo, incluso en un monitor secundario a 1-2 metros de distancia.

**Criterios:**
- Texto de estado: mínimo 12px, colores de alto contraste.
- La diferencia entre "Escuchando" y "Procesando" debe ser obvia (color + ícono).

---

### RNF2.3 — Persistencia

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF2.3 |
| **Categoría** | Datos |

**Descripción:**  
La geometría de ventana y preferencias de UI deben persistir entre sesiones sin depender de la nube.

**Criterios:**
- Archivos de configuración en `config/` (JSON legible).
- Si el archivo no existe, usar defaults razonables (1100x700 centrado).
- Si el monitor guardado ya no existe, usar defaults.

---

## 2. Criterios de Aceptación

- [ ] La ventana recuerda posición y tamaño al reiniciar la app.
- [ ] El label de estado distingue visualmente entre ≥3 estados del pipeline.
- [ ] El panel de acciones muestra al menos mensajes simulados de prueba.
- [ ] La UI no introduce nuevas dependencias pesadas.
- [ ] El rendimiento de la UI no degrada el FPS del juego/stream.
