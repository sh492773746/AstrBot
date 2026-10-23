# Bundled runtime

Imported from the user-supplied 网易_滑块_增强版 runtime. Only the protocol,
recognizer, local Node runtime, ONNX model and required templates are included.
The HTTP server, GUI, executable distribution and debug output are excluded.
The protocol's runtime directory is adapted to AstrBot's temporary directory.
Upstream source is kept separate from AstrBot's lint/format rules.

The supplied files do not include a separate license declaration. This notice
records provenance and does not relicense the supplied code, model or SDK asset.

## AstrBot Wrapper Operations

Updated October 2, 2026. Integration instructions live in the
[adapter guide](../../../../../../docs/en/platform/wangshangliao.md).
The wrapper is `captcha_worker.py`/`captcha.py`, not a separately exposed HTTP service.
Install `uv sync --extra wsl` and a working Node.js runtime on the target platform;
the worker loads its bundled ONNX model through OpenCV and runs serially on demand.
No GPU or Rust bridge is needed. A worker failure must leave manual verification available.

`ASTRBOT_YIDUN_SOLVER_URL` unset or `builtin` selects the bundled worker; an empty value
selects manual verification. External HTTP solver compatibility does not make the bundled
runtime a public service. Do not publish captcha inputs, solver debug output or deployment
protocol secrets. Keep the original provenance notice with copied runtime assets.
