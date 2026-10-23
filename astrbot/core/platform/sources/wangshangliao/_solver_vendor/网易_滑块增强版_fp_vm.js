const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DEFAULT_CORE_PATH = path.join(__dirname, "research", "yidun-single-check", "core-optimi.m25b40.v2.28.5.min.js");
const DEFAULT_REFERER = "https://dun.163.com/trial/jigsaw";
const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";
const sourceCache = new Map();

function argValue(name, fallback = "") {
  const index = process.argv.indexOf(name);
  if (index >= 0 && index + 1 < process.argv.length) return process.argv[index + 1];
  return fallback;
}

function hasArg(name) {
  return process.argv.includes(name);
}

function makeStorage() {
  const store = new Map();
  return {
    getItem(key) {
      return store.has(String(key)) ? store.get(String(key)) : null;
    },
    setItem(key, value) {
      store.set(String(key), String(value));
    },
    removeItem(key) {
      store.delete(String(key));
    },
  };
}

function makeElement(tagName) {
  const element = {
    tagName: String(tagName || "").toUpperCase(),
    style: {},
    children: [],
    offsetWidth: 100,
    offsetHeight: 20,
    innerHTML: "",
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      this.children = this.children.filter((item) => item !== child);
      return child;
    },
    setAttribute(name, value) {
      this[name] = String(value);
    },
    getAttribute(name) {
      return this[name] || "";
    },
    addBehavior() {},
    getContext(type) {
      if (type === "2d") {
        return {
          fillStyle: "",
          font: "",
          textBaseline: "",
          fillRect() {},
          fillText() {},
        };
      }
      return {
        TRIANGLE_STRIP: 5,
        ARRAY_BUFFER: 34962,
        STATIC_DRAW: 35044,
        FLOAT: 5126,
        VERTEX_SHADER: 35633,
        FRAGMENT_SHADER: 35632,
        createBuffer() {
          return {};
        },
        bindBuffer() {},
        bufferData() {},
        createProgram() {
          return {};
        },
        createShader() {
          return {};
        },
        shaderSource() {},
        compileShader() {},
        attachShader() {},
        linkProgram() {},
        useProgram() {},
        getAttribLocation() {
          return 0;
        },
        getUniformLocation() {
          return {};
        },
        enableVertexAttribArray() {},
        vertexAttribPointer() {},
        uniform2f() {},
        drawArrays() {},
        getSupportedExtensions() {
          return [];
        },
      };
    },
    toDataURL() {
      return "data:image/png;base64,";
    },
  };
  return element;
}

function locationFromReferer(referer) {
  try {
    const parsed = new URL(referer);
    return {
      href: referer,
      protocol: parsed.protocol,
      host: parsed.host || "",
      hostname: parsed.hostname || "",
    };
  } catch (_) {
    return {
      href: referer || "about:blank",
      protocol: "",
      host: "",
      hostname: "",
    };
  }
}

function buildContext(referer, userAgent) {
  const body = makeElement("body");
  const document = {
    cookie: "",
    body,
    documentElement: makeElement("html"),
    head: makeElement("head"),
    createElement: makeElement,
    getElementsByTagName(name) {
      if (name === "head") return [this.head];
      if (name === "body") return [this.body];
      return [];
    },
  };
  const location = locationFromReferer(referer);
  const navigator = {
    userAgent,
    platform: "Win32",
    language: "zh-CN",
    languages: ["zh-CN", "zh"],
    plugins: [],
    mimeTypes: [],
    doNotTrack: null,
    cpuClass: "x86",
  };
  const screen = {
    width: 1920,
    height: 1080,
    availWidth: 1920,
    availHeight: 1040,
    colorDepth: 24,
    pixelDepth: 24,
  };
  const window = {
    document,
    navigator,
    location,
    screen,
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
    Date,
    Math,
    JSON,
    Array,
    String,
    Number,
    Boolean,
    RegExp,
    Error,
    Object,
    Function,
    Float32Array,
    Int32Array,
    Uint8Array,
    setTimeout() {
      return 0;
    },
    clearTimeout() {},
    encodeURIComponent,
    decodeURIComponent,
    encodeURI,
    decodeURI,
    escape,
    unescape,
    parseInt,
    parseFloat,
    getComputedStyle(element) {
      return {
        getPropertyValue(name) {
          return (element && element.style && element.style[name]) || "";
        },
      };
    },
    openDatabase: undefined,
    indexedDB: {},
  };
  window.window = window;
  window.self = window;
  window.top = window;
  window.parent = window;
  document.defaultView = window;
  return vm.createContext({
    window,
    self: window,
    top: window,
    parent: window,
    document,
    navigator,
    location,
    screen,
    localStorage: window.localStorage,
    sessionStorage: window.sessionStorage,
    Date,
    Math,
    JSON,
    Array,
    String,
    Number,
    Boolean,
    RegExp,
    Error,
    Object,
    Function,
    Float32Array,
    Int32Array,
    Uint8Array,
    setTimeout: window.setTimeout,
    clearTimeout: window.clearTimeout,
    encodeURIComponent,
    decodeURIComponent,
    encodeURI,
    decodeURI,
    escape,
    unescape,
    parseInt,
    parseFloat,
    console: { log() {}, warn() {}, error() {} },
  });
}

function patchCoreSource(source, envFp, debugPlain) {
  const icpNeedle =
    "var _0x5f067c=!0x0,_0x548794=_0x7265d3,_0x155fa5=_0x1ab0f2();_0x155fa5&&(_0x548794[_0x2aea44[0x180]]=_0x155fa5),_0x155fa5=null,_0x548794[_0x2aea44[0x6e]]=_0x1b7775;";
  const icpReplacement =
    "var _0x5f067c=!0x0,_0x548794=_0x7265d3,_0x155fa5=null;_0x548794[_0x2aea44[0x6e]]=_0x1b7775;";
  if (source.includes(icpNeedle)) source = source.replace(icpNeedle, icpReplacement);

  const needle =
    "var _0x1b360a={};_0x1b360a['b']=!0x1,_0x1b360a['a']=!0x1;var _0x5ecc4a=new _0x6ce9ea(_0x1b360a)['get']();";
  const replacement =
    "var _0x1b360a={};_0x1b360a['b']=!0x1,_0x1b360a['a']=!0x1;var _0x5ecc4a=window.__YIDUN_ENV_FP__||new _0x6ce9ea(_0x1b360a)['get']();";
  if (envFp && source.includes(needle)) source = source.replace(needle, replacement);

  if (debugPlain) {
    const plainNeedle =
      "try{var _0x11ee50=_0x155fa5=_0x1c8398(_0x548794);";
    const plainReplacement =
      "try{window.__YIDUN_FP_PLAIN__=JSON.parse(JSON.stringify(_0x548794));var _0x11ee50=_0x155fa5=_0x1c8398(_0x548794);";
    if (source.includes(plainNeedle)) source = source.replace(plainNeedle, plainReplacement);
  }

  return source;
}

function cachedPatchedSource(corePath, envFp, debugPlain) {
  const key = `${corePath}\u0000${envFp || ""}\u0000${debugPlain ? "1" : "0"}`;
  if (!sourceCache.has(key)) {
    sourceCache.set(key, patchCoreSource(fs.readFileSync(corePath, "utf8"), envFp, debugPlain));
  }
  return sourceCache.get(key);
}

function envFpList(envFp) {
  return String(envFp || "")
    .trim()
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function generateFp(options = {}) {
  const corePath = options.corePath || options.core || DEFAULT_CORE_PATH;
  const referer = options.referer || DEFAULT_REFERER;
  const envFp = String(options.envFp || "").trim();
  const debugPlain = !!options.debugPlain;
  const userAgent = options.userAgent || DEFAULT_USER_AGENT;
  const vmTimeoutMs = Number.isFinite(Number(options.vmTimeoutMs)) ? Math.max(100, Number(options.vmTimeoutMs)) : 5000;
  const source = cachedPatchedSource(corePath, envFp, debugPlain);
  const context = buildContext(referer, userAgent);
  if (envFp) context.window.__YIDUN_ENV_FP__ = envFpList(envFp);
  vm.runInContext(source, context, { filename: path.basename(corePath), timeout: vmTimeoutMs });
  const fp = context.window.gdxidpyhxde || "";
  const match = /:(\d{10,})$/.exec(fp);
  return {
    ok: typeof fp === "string" && fp.length > 80 && !!match,
    fp,
    fpLength: fp.length,
    fpTimestampMs: match ? Number(match[1]) : null,
    source: options.worker ? "local_node_vm_worker" : "local_node_vm",
    envFp,
    plain: debugPlain ? context.window.__YIDUN_FP_PLAIN__ || null : undefined,
    corePath,
    referer,
  };
}

function writeResponse(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function handleWorkerLine(line) {
  if (!line.trim()) return;
  let request;
  try {
    request = JSON.parse(line);
  } catch (error) {
    writeResponse({ ok: false, id: null, source: "local_node_vm_worker", error: `invalid json: ${error.message || error}` });
    return;
  }
  try {
    writeResponse({ ...generateFp({ ...request, worker: true }), id: request.id });
  } catch (error) {
    writeResponse({
      ok: false,
      id: request.id,
      source: "local_node_vm_worker",
      error: error && error.message ? error.message : String(error),
    });
  }
}

function workerMain() {
  process.stdin.setEncoding("utf8");
  let buffer = "";
  process.stdin.on("data", (chunk) => {
    buffer += chunk;
    while (true) {
      const index = buffer.indexOf("\n");
      if (index < 0) break;
      const line = buffer.slice(0, index);
      buffer = buffer.slice(index + 1);
      handleWorkerLine(line);
    }
  });
  process.stdin.on("end", () => {
    if (buffer.trim()) handleWorkerLine(buffer);
  });
}

function main() {
  const result = generateFp({
    corePath: argValue("--core", DEFAULT_CORE_PATH),
    referer: argValue("--referer", DEFAULT_REFERER),
    envFp: argValue("--env-fp", "").trim(),
    debugPlain: hasArg("--debug-plain"),
    userAgent: argValue("--user-agent", DEFAULT_USER_AGENT),
  });
  console.log(JSON.stringify(result));
}

try {
  if (hasArg("--worker")) {
    workerMain();
  } else {
    main();
  }
} catch (error) {
  console.log(JSON.stringify({ ok: false, error: error && error.message ? error.message : String(error), source: "local_node_vm" }));
  process.exitCode = 1;
}
