/* Mi-Ripple Studio — preload.
 * The Gradio page runs fully self-contained; the preload only exposes
 * minimal, read-only app metadata for potential future UI extensions.
 */
"use strict";

const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("miRippleStudio", {
  name: "Mi-Ripple Studio",
  offline: true,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
});
