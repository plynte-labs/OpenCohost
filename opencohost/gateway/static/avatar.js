var Kd = Object.defineProperty, Zd = (r, e, t) => e in r ? Kd(r, e, { enumerable: !0, configurable: !0, writable: !0, value: t }) : r[e] = t, rr = (r, e, t) => Zd(r, typeof e != "symbol" ? e + "" : e, t);
const rc = "kira:auth", nc = "kira:speech", sc = "kira:state", $d = ["start", "boundary", "end"];
function Pa(r) {
  const e = r.detail;
  return e && typeof e == "object" ? e : null;
}
function Qd(r, e) {
  const t = (s) => {
    const a = Pa(s);
    a && (a.token === null ? e.onAuth(null) : typeof a.token == "string" && a.token && e.onAuth(a.token));
  }, i = (s) => {
    const a = Pa(s)?.phase;
    typeof a == "string" && $d.includes(a) && e.onSpeech(a);
  }, n = (s) => {
    const a = Pa(s)?.state;
    typeof a == "string" && a && e.onState(a);
  };
  return r.addEventListener(rc, t), r.addEventListener(nc, i), r.addEventListener(sc, n), () => {
    r.removeEventListener(rc, t), r.removeEventListener(nc, i), r.removeEventListener(sc, n);
  };
}
function Jd(r) {
  const { canvas: e, mouth: t, now: i } = r;
  let n = null, s = null, a = 0, o = "idle";
  const l = () => document.getElementById("kira-avatar-loading"), c = (f) => {
    e.dataset.avatarStatus = f, e.hidden = f === "unavailable";
    const v = l();
    v && f !== "loading" && (v.hidden = !0);
  }, h = (f) => {
    const v = l();
    v && (v.hidden = !1, v.textContent = f === null ? "..." : `${Math.round(f * 100)}%`);
  }, d = () => {
    if (a++, n) {
      try {
        n.dispose();
      } catch {
      }
      n = null;
    }
    s = null;
  }, u = (f) => {
    if (f === null) {
      d(), c("idle");
      return;
    }
    if (f === s) return;
    d(), s = f;
    const v = a;
    c("loading"), h(0), (async () => {
      try {
        const m = await r.fetchModel(f, (R) => {
          v === a && h(R);
        });
        if (v !== a) return;
        let g = !1;
        const E = await r.startStage(e, m, { mouth: t, now: i, getState: () => o, onDegraded: () => {
          g = !0;
        } });
        if (v !== a) {
          E.dispose();
          return;
        }
        n = E, c(g ? "degraded" : "ready");
      } catch {
        if (v !== a) return;
        s = null, c("unavailable");
      }
    })();
  };
  c("idle");
  const p = Qd(r.target, { onAuth: u, onSpeech: (f) => {
    f === "start" ? t.start(i()) : f === "boundary" ? t.pulse(i()) : t.end(i());
  }, onState: (f) => {
    o = f;
  } });
  return { destroy: () => {
    p(), d();
  } };
}
class Ca extends Error {
  constructor(e, t = `model request failed (${e})`) {
    super(t), this.status = e, this.name = "ModelError";
  }
}
async function ep(r, e, t = fetch) {
  let i;
  try {
    i = await t("/avatar/model", { headers: { Authorization: `Bearer ${r}` }, cache: "no-cache" });
  } catch {
    throw new Ca(0, "model request failed (network)");
  }
  if (!i.ok) throw new Ca(i.status);
  const n = Number(i.headers.get("content-length")) || 0;
  if (!i.body) {
    const h = await i.arrayBuffer();
    return e(1), h;
  }
  const s = i.body.getReader(), a = [];
  let o = 0;
  try {
    for (; ; ) {
      const { done: h, value: d } = await s.read();
      if (h) break;
      a.push(d), o += d.byteLength, e(n ? Math.min(1, o / n) : null);
    }
  } catch {
    throw new Ca(0, "model download interrupted");
  }
  const l = new Uint8Array(o);
  let c = 0;
  for (const h of a) l.set(h, c), c += h.byteLength;
  return l.buffer;
}
const tp = 0.75, ip = 0.55, rp = 0.2, ac = 0.15, np = 0.12, sp = 28, ap = 14, oc = (r, e, t) => Math.min(t, Math.max(e, r));
class op {
  constructor(e = Math.random) {
    rr(this, "on", !1), rr(this, "startedAt", 0), rr(this, "pulsedAt", Number.NEGATIVE_INFINITY), rr(this, "freq", 7), rr(this, "noise", 0.5), rr(this, "noiseSlot", -1), rr(this, "current", 0), rr(this, "last", null), this.random = e;
  }
  get speaking() {
    return this.on;
  }
  start(e) {
    this.on = !0, this.startedAt = e, this.freq = 6 + this.rand() * 3, this.noiseSlot = -1;
  }
  end(e) {
    this.on = !1;
  }
  pulse(e) {
    this.pulsedAt = e;
  }
  value(e) {
    if (!Number.isFinite(e)) return this.current;
    const t = this.last === null ? 0 : Math.max(0, e - this.last);
    this.last = e;
    let i = 0;
    if (this.on) {
      const s = Math.max(0, e - this.startedAt), a = Math.floor(s / np);
      a !== this.noiseSlot && (this.noiseSlot = a, this.noise = this.rand());
      const o = 0.5 + 0.5 * Math.sin(2 * Math.PI * this.freq * s);
      i = ip * o * (0.55 + 0.45 * this.noise);
      const l = e - this.pulsedAt;
      l >= 0 && l < ac && (i += rp * (1 - l / ac));
    }
    const n = i > this.current ? sp : ap;
    return this.current += (i - this.current) * (1 - Math.exp(-n * t)), !this.on && this.current < 2e-3 && (this.current = 0), this.current = oc(this.current, 0, tp), this.current;
  }
  rand() {
    return oc(this.random(), 0, 1);
  }
}
const Ir = "186", lp = 0, lc = 1, cp = 2, qs = 1, hp = 2, qn = 3, fr = 0, Gt = 1, ai = 2, $i = 0, Qn = 1, cc = 2, hc = 3, uc = 4, up = 5, nn = 100, dp = 101, pp = 102, fp = 103, mp = 104, gp = 200, _p = 201, vp = 202, xp = 203, yu = 204, Su = 205, Mp = 206, yp = 207, Sp = 208, Tp = 209, Ep = 210, bp = 211, wp = 212, Ap = 213, Rp = 214, Oo = 0, Fo = 1, Bo = 2, is = 3, Vo = 4, Ho = 5, ko = 6, zo = 7, Tu = 0, Pp = 1, Cp = 2, Ii = 0, Eu = 1, bu = 2, wu = 3, Au = 4, Ru = 5, Pu = 6, Cu = 7, dc = "attached", Lp = "detached", Lu = 300, Nr = 301, gn = 302, La = 303, Ia = 304, xa = 306, _n = 1e3, Ci = 1001, na = 1002, Rt = 1003, Iu = 1004, Yn = 1005, Pt = 1006, Ys = 1007, Yi = 1008, $t = 1009, Nu = 1010, Uu = 1011, rs = 1012, Dl = 1013, Ni = 1014, oi = 1015, Ui = 1016, Ol = 1017, Fl = 1018, ns = 1020, Du = 35902, Ou = 35899, Fu = 1021, Bu = 1022, li = 1023, Ji = 1026, Cr = 1027, Bl = 1028, Vl = 1029, Ur = 1030, Hl = 1031, kl = 1033, Ks = 33776, Zs = 33777, $s = 33778, Qs = 33779, Go = 35840, Wo = 35841, Xo = 35842, jo = 35843, qo = 36196, Yo = 37492, Ko = 37496, Zo = 37488, $o = 37489, sa = 37490, Qo = 37491, Jo = 37808, el = 37809, tl = 37810, il = 37811, rl = 37812, nl = 37813, sl = 37814, al = 37815, ol = 37816, ll = 37817, cl = 37818, hl = 37819, ul = 37820, dl = 37821, pl = 36492, fl = 36494, ml = 36495, gl = 36283, _l = 36284, aa = 36285, vl = 36286, sn = 2200, Kn = 2201, Ip = 2202, ss = 2300, as = 2301, Na = 2302, pc = 2303, hn = 2400, un = 2401, oa = 2402, zl = 2500, Np = 2501, Up = 0, Vu = 1, xl = 2, Dp = 3200, la = 0, Op = 1, pr = "", Ot = "srgb", ei = "srgb-linear", ca = "linear", ot = "srgb", Ua = 7680, Fp = 519, Bp = 512, Vp = 513, Hp = 514, Gl = 515, kp = 516, zp = 517, Wl = 518, Gp = 519, Hu = 35044, Wp = 35048, fc = "300 es", Li = 2e3, os = 2001;
function Xp(r) {
  for (let e = r.length - 1; e >= 0; --e) if (r[e] >= 65535) return !0;
  return !1;
}
function jp(r) {
  return ArrayBuffer.isView(r) && !(r instanceof DataView);
}
function ls(r) {
  return document.createElementNS("http://www.w3.org/1999/xhtml", r);
}
function qp() {
  const r = ls("canvas");
  return r.style.display = "block", r;
}
const mc = {};
function ha(...r) {
  const e = "THREE." + r.shift();
  console.log(e, ...r);
}
function ku(r) {
  const e = r[0];
  if (typeof e == "string" && e.startsWith("TSL:")) {
    const t = r[1];
    t && t.isStackTrace ? r[0] += " " + t.getLocation() : r[1] = 'Stack trace not available. Enable "THREE.Node.captureStackTrace" to capture stack traces.';
  }
  return r;
}
function Re(...r) {
  r = ku(r);
  const e = "THREE." + r.shift();
  {
    const t = r[0];
    t && t.isStackTrace ? console.warn(t.getError(e)) : console.warn(e, ...r);
  }
}
function Be(...r) {
  r = ku(r);
  const e = "THREE." + r.shift();
  {
    const t = r[0];
    t && t.isStackTrace ? console.error(t.getError(e)) : console.error(e, ...r);
  }
}
function pn(...r) {
  const e = r.join(" ");
  e in mc || (mc[e] = !0, Re(...r));
}
function Yp(r, e, t) {
  return new Promise(function(i, n) {
    function s() {
      switch (r.clientWaitSync(e, r.SYNC_FLUSH_COMMANDS_BIT, 0)) {
        case r.WAIT_FAILED:
          n();
          break;
        case r.TIMEOUT_EXPIRED:
          setTimeout(s, t);
          break;
        default:
          i();
      }
    }
    setTimeout(s, t);
  });
}
const Kp = { [Oo]: Fo, [Bo]: ko, [Vo]: zo, [is]: Ho, [Fo]: Oo, [ko]: Bo, [zo]: Vo, [Ho]: is };
class mr {
  addEventListener(e, t) {
    this._listeners === void 0 && (this._listeners = {});
    const i = this._listeners;
    i[e] === void 0 && (i[e] = []), i[e].indexOf(t) === -1 && i[e].push(t);
  }
  hasEventListener(e, t) {
    const i = this._listeners;
    return i === void 0 ? !1 : i[e] !== void 0 && i[e].indexOf(t) !== -1;
  }
  removeEventListener(e, t) {
    const i = this._listeners;
    if (i === void 0) return;
    const n = i[e];
    if (n !== void 0) {
      const s = n.indexOf(t);
      s !== -1 && n.splice(s, 1);
    }
  }
  dispatchEvent(e) {
    const t = this._listeners;
    if (t === void 0) return;
    const i = t[e.type];
    if (i !== void 0) {
      e.target = this;
      const n = i.slice(0);
      for (let s = 0, a = n.length; s < a; s++) n[s].call(this, e);
      e.target = null;
    }
  }
}
const Ft = ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "0a", "0b", "0c", "0d", "0e", "0f", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "1a", "1b", "1c", "1d", "1e", "1f", "20", "21", "22", "23", "24", "25", "26", "27", "28", "29", "2a", "2b", "2c", "2d", "2e", "2f", "30", "31", "32", "33", "34", "35", "36", "37", "38", "39", "3a", "3b", "3c", "3d", "3e", "3f", "40", "41", "42", "43", "44", "45", "46", "47", "48", "49", "4a", "4b", "4c", "4d", "4e", "4f", "50", "51", "52", "53", "54", "55", "56", "57", "58", "59", "5a", "5b", "5c", "5d", "5e", "5f", "60", "61", "62", "63", "64", "65", "66", "67", "68", "69", "6a", "6b", "6c", "6d", "6e", "6f", "70", "71", "72", "73", "74", "75", "76", "77", "78", "79", "7a", "7b", "7c", "7d", "7e", "7f", "80", "81", "82", "83", "84", "85", "86", "87", "88", "89", "8a", "8b", "8c", "8d", "8e", "8f", "90", "91", "92", "93", "94", "95", "96", "97", "98", "99", "9a", "9b", "9c", "9d", "9e", "9f", "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "a9", "aa", "ab", "ac", "ad", "ae", "af", "b0", "b1", "b2", "b3", "b4", "b5", "b6", "b7", "b8", "b9", "ba", "bb", "bc", "bd", "be", "bf", "c0", "c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8", "c9", "ca", "cb", "cc", "cd", "ce", "cf", "d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7", "d8", "d9", "da", "db", "dc", "dd", "de", "df", "e0", "e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8", "e9", "ea", "eb", "ec", "ed", "ee", "ef", "f0", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "fa", "fb", "fc", "fd", "fe", "ff"];
let gc = 1234567;
const Jn = Math.PI / 180, vn = 180 / Math.PI;
function gi() {
  const r = Math.random() * 4294967295 | 0, e = Math.random() * 4294967295 | 0, t = Math.random() * 4294967295 | 0, i = Math.random() * 4294967295 | 0;
  return (Ft[r & 255] + Ft[r >> 8 & 255] + Ft[r >> 16 & 255] + Ft[r >> 24 & 255] + "-" + Ft[e & 255] + Ft[e >> 8 & 255] + "-" + Ft[e >> 16 & 15 | 64] + Ft[e >> 24 & 255] + "-" + Ft[t & 63 | 128] + Ft[t >> 8 & 255] + "-" + Ft[t >> 16 & 255] + Ft[t >> 24 & 255] + Ft[i & 255] + Ft[i >> 8 & 255] + Ft[i >> 16 & 255] + Ft[i >> 24 & 255]).toLowerCase();
}
function $e(r, e, t) {
  return Math.max(e, Math.min(t, r));
}
function Xl(r, e) {
  return (r % e + e) % e;
}
function Zp(r, e, t, i, n) {
  return i + (r - e) * (n - i) / (t - e);
}
function $p(r, e, t) {
  return r !== e ? (t - r) / (e - r) : 0;
}
function es(r, e, t) {
  return (1 - t) * r + t * e;
}
function Qp(r, e, t, i) {
  return es(r, e, 1 - Math.exp(-t * i));
}
function Jp(r, e = 1) {
  return e - Math.abs(Xl(r, e * 2) - e);
}
function ef(r, e, t) {
  return r <= e ? 0 : r >= t ? 1 : (r = (r - e) / (t - e), r * r * (3 - 2 * r));
}
function tf(r, e, t) {
  return r <= e ? 0 : r >= t ? 1 : (r = (r - e) / (t - e), r * r * r * (r * (r * 6 - 15) + 10));
}
function rf(r, e) {
  return r + Math.floor(Math.random() * (e - r + 1));
}
function nf(r, e) {
  return r + Math.random() * (e - r);
}
function sf(r) {
  return r * (0.5 - Math.random());
}
function af(r) {
  r !== void 0 && (gc = r);
  let e = gc += 1831565813;
  return e = Math.imul(e ^ e >>> 15, e | 1), e ^= e + Math.imul(e ^ e >>> 7, e | 61), ((e ^ e >>> 14) >>> 0) / 4294967296;
}
function of(r) {
  return r * Jn;
}
function lf(r) {
  return r * vn;
}
function cf(r) {
  return r > 0 && Number.isInteger(r) && 2 ** Math.round(Math.log2(r)) === r;
}
function hf(r) {
  return Math.pow(2, Math.ceil(Math.log(r) / Math.LN2));
}
function uf(r) {
  return Math.pow(2, Math.floor(Math.log(r) / Math.LN2));
}
function df(r, e, t, i, n) {
  const s = Math.cos, a = Math.sin, o = s(t / 2), l = a(t / 2), c = s((e + i) / 2), h = a((e + i) / 2), d = s((e - i) / 2), u = a((e - i) / 2), p = s((i - e) / 2), f = a((i - e) / 2);
  switch (n) {
    case "XYX":
      r.set(o * h, l * d, l * u, o * c);
      break;
    case "YZY":
      r.set(l * u, o * h, l * d, o * c);
      break;
    case "ZXZ":
      r.set(l * d, l * u, o * h, o * c);
      break;
    case "XZX":
      r.set(o * h, l * f, l * p, o * c);
      break;
    case "YXY":
      r.set(l * p, o * h, l * f, o * c);
      break;
    case "ZYZ":
      r.set(l * f, l * p, o * h, o * c);
      break;
    default:
      Re("MathUtils: .setQuaternionFromProperEuler() encountered an unknown order: " + n);
  }
}
function pi(r, e) {
  switch (e.constructor) {
    case Float32Array:
      return r;
    case Uint32Array:
      return r / 4294967295;
    case Uint16Array:
      return r / 65535;
    case Uint8Array:
    case Uint8ClampedArray:
      return r / 255;
    case Int32Array:
      return Math.max(r / 2147483647, -1);
    case Int16Array:
      return Math.max(r / 32767, -1);
    case Int8Array:
      return Math.max(r / 127, -1);
    default:
      throw new Error("THREE.MathUtils: Invalid component type.");
  }
}
function lt(r, e) {
  switch (e.constructor) {
    case Float32Array:
      return r;
    case Uint32Array:
      return Math.round(r * 4294967295);
    case Uint16Array:
      return Math.round(r * 65535);
    case Uint8Array:
    case Uint8ClampedArray:
      return Math.round(r * 255);
    case Int32Array:
      return Math.round(r * 2147483647);
    case Int16Array:
      return Math.round(r * 32767);
    case Int8Array:
      return Math.round(r * 127);
    default:
      throw new Error("THREE.MathUtils: Invalid component type.");
  }
}
const mt = { DEG2RAD: Jn, RAD2DEG: vn, generateUUID: gi, clamp: $e, euclideanModulo: Xl, mapLinear: Zp, inverseLerp: $p, lerp: es, damp: Qp, pingpong: Jp, smoothstep: ef, smootherstep: tf, randInt: rf, randFloat: nf, randFloatSpread: sf, seededRandom: af, degToRad: of, radToDeg: lf, isPowerOfTwo: cf, ceilPowerOfTwo: hf, floorPowerOfTwo: uf, setQuaternionFromProperEuler: df, normalize: lt, denormalize: pi }, zu = class {
  constructor(e = 0, t = 0) {
    this.x = e, this.y = t;
  }
  get width() {
    return this.x;
  }
  set width(e) {
    this.x = e;
  }
  get height() {
    return this.y;
  }
  set height(e) {
    this.y = e;
  }
  set(e, t) {
    return this.x = e, this.y = t, this;
  }
  setScalar(e) {
    return this.x = e, this.y = e, this;
  }
  setX(e) {
    return this.x = e, this;
  }
  setY(e) {
    return this.y = e, this;
  }
  setComponent(e, t) {
    switch (e) {
      case 0:
        this.x = t;
        break;
      case 1:
        this.y = t;
        break;
      default:
        throw new Error("THREE.Vector2: index is out of range: " + e);
    }
    return this;
  }
  getComponent(e) {
    switch (e) {
      case 0:
        return this.x;
      case 1:
        return this.y;
      default:
        throw new Error("THREE.Vector2: index is out of range: " + e);
    }
  }
  clone() {
    return new this.constructor(this.x, this.y);
  }
  copy(e) {
    return this.x = e.x, this.y = e.y, this;
  }
  add(e) {
    return this.x += e.x, this.y += e.y, this;
  }
  addScalar(e) {
    return this.x += e, this.y += e, this;
  }
  addVectors(e, t) {
    return this.x = e.x + t.x, this.y = e.y + t.y, this;
  }
  addScaledVector(e, t) {
    return this.x += e.x * t, this.y += e.y * t, this;
  }
  sub(e) {
    return this.x -= e.x, this.y -= e.y, this;
  }
  subScalar(e) {
    return this.x -= e, this.y -= e, this;
  }
  subVectors(e, t) {
    return this.x = e.x - t.x, this.y = e.y - t.y, this;
  }
  multiply(e) {
    return this.x *= e.x, this.y *= e.y, this;
  }
  multiplyScalar(e) {
    return this.x *= e, this.y *= e, this;
  }
  divide(e) {
    return this.x /= e.x, this.y /= e.y, this;
  }
  divideScalar(e) {
    return this.multiplyScalar(1 / e);
  }
  applyMatrix3(e) {
    const t = this.x, i = this.y, n = e.elements;
    return this.x = n[0] * t + n[3] * i + n[6], this.y = n[1] * t + n[4] * i + n[7], this;
  }
  min(e) {
    return this.x = Math.min(this.x, e.x), this.y = Math.min(this.y, e.y), this;
  }
  max(e) {
    return this.x = Math.max(this.x, e.x), this.y = Math.max(this.y, e.y), this;
  }
  clamp(e, t) {
    return this.x = $e(this.x, e.x, t.x), this.y = $e(this.y, e.y, t.y), this;
  }
  clampScalar(e, t) {
    return this.x = $e(this.x, e, t), this.y = $e(this.y, e, t), this;
  }
  clampLength(e, t) {
    const i = this.length();
    return this.divideScalar(i || 1).multiplyScalar($e(i, e, t));
  }
  floor() {
    return this.x = Math.floor(this.x), this.y = Math.floor(this.y), this;
  }
  ceil() {
    return this.x = Math.ceil(this.x), this.y = Math.ceil(this.y), this;
  }
  round() {
    return this.x = Math.round(this.x), this.y = Math.round(this.y), this;
  }
  roundToZero() {
    return this.x = Math.trunc(this.x), this.y = Math.trunc(this.y), this;
  }
  negate() {
    return this.x = -this.x, this.y = -this.y, this;
  }
  dot(e) {
    return this.x * e.x + this.y * e.y;
  }
  cross(e) {
    return this.x * e.y - this.y * e.x;
  }
  lengthSq() {
    return this.x * this.x + this.y * this.y;
  }
  length() {
    return Math.sqrt(this.x * this.x + this.y * this.y);
  }
  manhattanLength() {
    return Math.abs(this.x) + Math.abs(this.y);
  }
  normalize() {
    return this.divideScalar(this.length() || 1);
  }
  angle() {
    return Math.atan2(-this.y, -this.x) + Math.PI;
  }
  angleTo(e) {
    const t = Math.sqrt(this.lengthSq() * e.lengthSq());
    if (t === 0) return Math.PI / 2;
    const i = this.dot(e) / t;
    return Math.acos($e(i, -1, 1));
  }
  distanceTo(e) {
    return Math.sqrt(this.distanceToSquared(e));
  }
  distanceToSquared(e) {
    const t = this.x - e.x, i = this.y - e.y;
    return t * t + i * i;
  }
  manhattanDistanceTo(e) {
    return Math.abs(this.x - e.x) + Math.abs(this.y - e.y);
  }
  setLength(e) {
    return this.normalize().multiplyScalar(e);
  }
  lerp(e, t) {
    return this.x += (e.x - this.x) * t, this.y += (e.y - this.y) * t, this;
  }
  lerpVectors(e, t, i) {
    return this.x = e.x + (t.x - e.x) * i, this.y = e.y + (t.y - e.y) * i, this;
  }
  equals(e) {
    return e.x === this.x && e.y === this.y;
  }
  fromArray(e, t = 0) {
    return this.x = e[t], this.y = e[t + 1], this;
  }
  toArray(e = [], t = 0) {
    return e[t] = this.x, e[t + 1] = this.y, e;
  }
  fromBufferAttribute(e, t) {
    return this.x = e.getX(t), this.y = e.getY(t), this;
  }
  rotateAround(e, t) {
    const i = Math.cos(t), n = Math.sin(t), s = this.x - e.x, a = this.y - e.y;
    return this.x = s * i - a * n + e.x, this.y = s * n + a * i + e.y, this;
  }
  random() {
    return this.x = Math.random(), this.y = Math.random(), this;
  }
  *[Symbol.iterator]() {
    yield this.x, yield this.y;
  }
};
zu.prototype.isVector2 = !0;
let We = zu;
class Ce {
  constructor(e = 0, t = 0, i = 0, n = 1) {
    this.isQuaternion = !0, this._x = e, this._y = t, this._z = i, this._w = n;
  }
  static slerpFlat(e, t, i, n, s, a, o) {
    let l = i[n + 0], c = i[n + 1], h = i[n + 2], d = i[n + 3], u = s[a + 0], p = s[a + 1], f = s[a + 2], v = s[a + 3];
    if (d !== v || l !== u || c !== p || h !== f) {
      let m = l * u + c * p + h * f + d * v;
      m < 0 && (u = -u, p = -p, f = -f, v = -v, m = -m);
      let g = 1 - o;
      if (m < 0.9995) {
        const E = Math.acos(m), R = Math.sin(E);
        g = Math.sin(g * E) / R, o = Math.sin(o * E) / R, l = l * g + u * o, c = c * g + p * o, h = h * g + f * o, d = d * g + v * o;
      } else {
        l = l * g + u * o, c = c * g + p * o, h = h * g + f * o, d = d * g + v * o;
        const E = 1 / Math.sqrt(l * l + c * c + h * h + d * d);
        l *= E, c *= E, h *= E, d *= E;
      }
    }
    e[t] = l, e[t + 1] = c, e[t + 2] = h, e[t + 3] = d;
  }
  static multiplyQuaternionsFlat(e, t, i, n, s, a) {
    const o = i[n], l = i[n + 1], c = i[n + 2], h = i[n + 3], d = s[a], u = s[a + 1], p = s[a + 2], f = s[a + 3];
    return e[t] = o * f + h * d + l * p - c * u, e[t + 1] = l * f + h * u + c * d - o * p, e[t + 2] = c * f + h * p + o * u - l * d, e[t + 3] = h * f - o * d - l * u - c * p, e;
  }
  get x() {
    return this._x;
  }
  set x(e) {
    this._x = e, this._onChangeCallback();
  }
  get y() {
    return this._y;
  }
  set y(e) {
    this._y = e, this._onChangeCallback();
  }
  get z() {
    return this._z;
  }
  set z(e) {
    this._z = e, this._onChangeCallback();
  }
  get w() {
    return this._w;
  }
  set w(e) {
    this._w = e, this._onChangeCallback();
  }
  set(e, t, i, n) {
    return this._x = e, this._y = t, this._z = i, this._w = n, this._onChangeCallback(), this;
  }
  clone() {
    return new this.constructor(this._x, this._y, this._z, this._w);
  }
  copy(e) {
    return this._x = e.x, this._y = e.y, this._z = e.z, this._w = e.w, this._onChangeCallback(), this;
  }
  setFromEuler(e, t = !0) {
    const i = e._x, n = e._y, s = e._z, a = e._order, o = Math.cos, l = Math.sin, c = o(i / 2), h = o(n / 2), d = o(s / 2), u = l(i / 2), p = l(n / 2), f = l(s / 2);
    switch (a) {
      case "XYZ":
        this._x = u * h * d + c * p * f, this._y = c * p * d - u * h * f, this._z = c * h * f + u * p * d, this._w = c * h * d - u * p * f;
        break;
      case "YXZ":
        this._x = u * h * d + c * p * f, this._y = c * p * d - u * h * f, this._z = c * h * f - u * p * d, this._w = c * h * d + u * p * f;
        break;
      case "ZXY":
        this._x = u * h * d - c * p * f, this._y = c * p * d + u * h * f, this._z = c * h * f + u * p * d, this._w = c * h * d - u * p * f;
        break;
      case "ZYX":
        this._x = u * h * d - c * p * f, this._y = c * p * d + u * h * f, this._z = c * h * f - u * p * d, this._w = c * h * d + u * p * f;
        break;
      case "YZX":
        this._x = u * h * d + c * p * f, this._y = c * p * d + u * h * f, this._z = c * h * f - u * p * d, this._w = c * h * d - u * p * f;
        break;
      case "XZY":
        this._x = u * h * d - c * p * f, this._y = c * p * d - u * h * f, this._z = c * h * f + u * p * d, this._w = c * h * d + u * p * f;
        break;
      default:
        Re("Quaternion: .setFromEuler() encountered an unknown order: " + a);
    }
    return t === !0 && this._onChangeCallback(), this;
  }
  setFromAxisAngle(e, t) {
    const i = t / 2, n = Math.sin(i);
    return this._x = e.x * n, this._y = e.y * n, this._z = e.z * n, this._w = Math.cos(i), this._onChangeCallback(), this;
  }
  setFromRotationMatrix(e) {
    const t = e.elements, i = t[0], n = t[4], s = t[8], a = t[1], o = t[5], l = t[9], c = t[2], h = t[6], d = t[10], u = i + o + d;
    if (u > 0) {
      const p = 0.5 / Math.sqrt(u + 1);
      this._w = 0.25 / p, this._x = (h - l) * p, this._y = (s - c) * p, this._z = (a - n) * p;
    } else if (i > o && i > d) {
      const p = 2 * Math.sqrt(1 + i - o - d);
      this._w = (h - l) / p, this._x = 0.25 * p, this._y = (n + a) / p, this._z = (s + c) / p;
    } else if (o > d) {
      const p = 2 * Math.sqrt(1 + o - i - d);
      this._w = (s - c) / p, this._x = (n + a) / p, this._y = 0.25 * p, this._z = (l + h) / p;
    } else {
      const p = 2 * Math.sqrt(1 + d - i - o);
      this._w = (a - n) / p, this._x = (s + c) / p, this._y = (l + h) / p, this._z = 0.25 * p;
    }
    return this._onChangeCallback(), this;
  }
  setFromUnitVectors(e, t) {
    let i = e.dot(t) + 1;
    return i < 1e-8 ? (i = 0, Math.abs(e.x) > Math.abs(e.z) ? (this._x = -e.y, this._y = e.x, this._z = 0, this._w = i) : (this._x = 0, this._y = -e.z, this._z = e.y, this._w = i)) : (this._x = e.y * t.z - e.z * t.y, this._y = e.z * t.x - e.x * t.z, this._z = e.x * t.y - e.y * t.x, this._w = i), this.normalize();
  }
  angleTo(e) {
    return 2 * Math.acos(Math.abs($e(this.dot(e), -1, 1)));
  }
  rotateTowards(e, t) {
    const i = this.angleTo(e);
    if (i === 0) return this;
    const n = Math.min(1, t / i);
    return this.slerp(e, n), this;
  }
  identity() {
    return this.set(0, 0, 0, 1);
  }
  invert() {
    return this.conjugate();
  }
  conjugate() {
    return this._x *= -1, this._y *= -1, this._z *= -1, this._onChangeCallback(), this;
  }
  dot(e) {
    return this._x * e._x + this._y * e._y + this._z * e._z + this._w * e._w;
  }
  lengthSq() {
    return this._x * this._x + this._y * this._y + this._z * this._z + this._w * this._w;
  }
  length() {
    return Math.sqrt(this._x * this._x + this._y * this._y + this._z * this._z + this._w * this._w);
  }
  normalize() {
    let e = this.length();
    return e === 0 ? (this._x = 0, this._y = 0, this._z = 0, this._w = 1) : (e = 1 / e, this._x = this._x * e, this._y = this._y * e, this._z = this._z * e, this._w = this._w * e), this._onChangeCallback(), this;
  }
  multiply(e) {
    return this.multiplyQuaternions(this, e);
  }
  premultiply(e) {
    return this.multiplyQuaternions(e, this);
  }
  multiplyQuaternions(e, t) {
    const i = e._x, n = e._y, s = e._z, a = e._w, o = t._x, l = t._y, c = t._z, h = t._w;
    return this._x = i * h + a * o + n * c - s * l, this._y = n * h + a * l + s * o - i * c, this._z = s * h + a * c + i * l - n * o, this._w = a * h - i * o - n * l - s * c, this._onChangeCallback(), this;
  }
  slerp(e, t) {
    let i = e._x, n = e._y, s = e._z, a = e._w, o = this.dot(e);
    o < 0 && (i = -i, n = -n, s = -s, a = -a, o = -o);
    let l = 1 - t;
    if (o < 0.9995) {
      const c = Math.acos(o), h = Math.sin(c);
      l = Math.sin(l * c) / h, t = Math.sin(t * c) / h, this._x = this._x * l + i * t, this._y = this._y * l + n * t, this._z = this._z * l + s * t, this._w = this._w * l + a * t, this._onChangeCallback();
    } else this._x = this._x * l + i * t, this._y = this._y * l + n * t, this._z = this._z * l + s * t, this._w = this._w * l + a * t, this.normalize();
    return this;
  }
  slerpQuaternions(e, t, i) {
    return this.copy(e).slerp(t, i);
  }
  random() {
    const e = 2 * Math.PI * Math.random(), t = 2 * Math.PI * Math.random(), i = Math.random(), n = Math.sqrt(1 - i), s = Math.sqrt(i);
    return this.set(n * Math.sin(e), n * Math.cos(e), s * Math.sin(t), s * Math.cos(t));
  }
  equals(e) {
    return e._x === this._x && e._y === this._y && e._z === this._z && e._w === this._w;
  }
  fromArray(e, t = 0) {
    return this._x = e[t], this._y = e[t + 1], this._z = e[t + 2], this._w = e[t + 3], this._onChangeCallback(), this;
  }
  toArray(e = [], t = 0) {
    return e[t] = this._x, e[t + 1] = this._y, e[t + 2] = this._z, e[t + 3] = this._w, e;
  }
  fromBufferAttribute(e, t) {
    return this._x = e.getX(t), this._y = e.getY(t), this._z = e.getZ(t), this._w = e.getW(t), this._onChangeCallback(), this;
  }
  toJSON() {
    return this.toArray();
  }
  _onChange(e) {
    return this._onChangeCallback = e, this;
  }
  _onChangeCallback() {
  }
  *[Symbol.iterator]() {
    yield this._x, yield this._y, yield this._z, yield this._w;
  }
}
const Gu = class {
  constructor(e = 0, t = 0, i = 0) {
    this.x = e, this.y = t, this.z = i;
  }
  set(e, t, i) {
    return i === void 0 && (i = this.z), this.x = e, this.y = t, this.z = i, this;
  }
  setScalar(e) {
    return this.x = e, this.y = e, this.z = e, this;
  }
  setX(e) {
    return this.x = e, this;
  }
  setY(e) {
    return this.y = e, this;
  }
  setZ(e) {
    return this.z = e, this;
  }
  setComponent(e, t) {
    switch (e) {
      case 0:
        this.x = t;
        break;
      case 1:
        this.y = t;
        break;
      case 2:
        this.z = t;
        break;
      default:
        throw new Error("THREE.Vector3: index is out of range: " + e);
    }
    return this;
  }
  getComponent(e) {
    switch (e) {
      case 0:
        return this.x;
      case 1:
        return this.y;
      case 2:
        return this.z;
      default:
        throw new Error("THREE.Vector3: index is out of range: " + e);
    }
  }
  clone() {
    return new this.constructor(this.x, this.y, this.z);
  }
  copy(e) {
    return this.x = e.x, this.y = e.y, this.z = e.z, this;
  }
  add(e) {
    return this.x += e.x, this.y += e.y, this.z += e.z, this;
  }
  addScalar(e) {
    return this.x += e, this.y += e, this.z += e, this;
  }
  addVectors(e, t) {
    return this.x = e.x + t.x, this.y = e.y + t.y, this.z = e.z + t.z, this;
  }
  addScaledVector(e, t) {
    return this.x += e.x * t, this.y += e.y * t, this.z += e.z * t, this;
  }
  sub(e) {
    return this.x -= e.x, this.y -= e.y, this.z -= e.z, this;
  }
  subScalar(e) {
    return this.x -= e, this.y -= e, this.z -= e, this;
  }
  subVectors(e, t) {
    return this.x = e.x - t.x, this.y = e.y - t.y, this.z = e.z - t.z, this;
  }
  multiply(e) {
    return this.x *= e.x, this.y *= e.y, this.z *= e.z, this;
  }
  multiplyScalar(e) {
    return this.x *= e, this.y *= e, this.z *= e, this;
  }
  multiplyVectors(e, t) {
    return this.x = e.x * t.x, this.y = e.y * t.y, this.z = e.z * t.z, this;
  }
  applyEuler(e) {
    return this.applyQuaternion(_c.setFromEuler(e));
  }
  applyAxisAngle(e, t) {
    return this.applyQuaternion(_c.setFromAxisAngle(e, t));
  }
  applyMatrix3(e) {
    const t = this.x, i = this.y, n = this.z, s = e.elements;
    return this.x = s[0] * t + s[3] * i + s[6] * n, this.y = s[1] * t + s[4] * i + s[7] * n, this.z = s[2] * t + s[5] * i + s[8] * n, this;
  }
  applyNormalMatrix(e) {
    return this.applyMatrix3(e).normalize();
  }
  applyMatrix4(e) {
    const t = this.x, i = this.y, n = this.z, s = e.elements, a = 1 / (s[3] * t + s[7] * i + s[11] * n + s[15]);
    return this.x = (s[0] * t + s[4] * i + s[8] * n + s[12]) * a, this.y = (s[1] * t + s[5] * i + s[9] * n + s[13]) * a, this.z = (s[2] * t + s[6] * i + s[10] * n + s[14]) * a, this;
  }
  applyQuaternion(e) {
    const t = this.x, i = this.y, n = this.z, s = e.x, a = e.y, o = e.z, l = e.w, c = 2 * (a * n - o * i), h = 2 * (o * t - s * n), d = 2 * (s * i - a * t);
    return this.x = t + l * c + a * d - o * h, this.y = i + l * h + o * c - s * d, this.z = n + l * d + s * h - a * c, this;
  }
  project(e) {
    return this.applyMatrix4(e.matrixWorldInverse).applyMatrix4(e.projectionMatrix);
  }
  unproject(e) {
    return this.applyMatrix4(e.projectionMatrixInverse).applyMatrix4(e.matrixWorld);
  }
  transformDirection(e) {
    const t = this.x, i = this.y, n = this.z, s = e.elements;
    return this.x = s[0] * t + s[4] * i + s[8] * n, this.y = s[1] * t + s[5] * i + s[9] * n, this.z = s[2] * t + s[6] * i + s[10] * n, this.normalize();
  }
  divide(e) {
    return this.x /= e.x, this.y /= e.y, this.z /= e.z, this;
  }
  divideScalar(e) {
    return this.multiplyScalar(1 / e);
  }
  min(e) {
    return this.x = Math.min(this.x, e.x), this.y = Math.min(this.y, e.y), this.z = Math.min(this.z, e.z), this;
  }
  max(e) {
    return this.x = Math.max(this.x, e.x), this.y = Math.max(this.y, e.y), this.z = Math.max(this.z, e.z), this;
  }
  clamp(e, t) {
    return this.x = $e(this.x, e.x, t.x), this.y = $e(this.y, e.y, t.y), this.z = $e(this.z, e.z, t.z), this;
  }
  clampScalar(e, t) {
    return this.x = $e(this.x, e, t), this.y = $e(this.y, e, t), this.z = $e(this.z, e, t), this;
  }
  clampLength(e, t) {
    const i = this.length();
    return this.divideScalar(i || 1).multiplyScalar($e(i, e, t));
  }
  floor() {
    return this.x = Math.floor(this.x), this.y = Math.floor(this.y), this.z = Math.floor(this.z), this;
  }
  ceil() {
    return this.x = Math.ceil(this.x), this.y = Math.ceil(this.y), this.z = Math.ceil(this.z), this;
  }
  round() {
    return this.x = Math.round(this.x), this.y = Math.round(this.y), this.z = Math.round(this.z), this;
  }
  roundToZero() {
    return this.x = Math.trunc(this.x), this.y = Math.trunc(this.y), this.z = Math.trunc(this.z), this;
  }
  negate() {
    return this.x = -this.x, this.y = -this.y, this.z = -this.z, this;
  }
  dot(e) {
    return this.x * e.x + this.y * e.y + this.z * e.z;
  }
  lengthSq() {
    return this.x * this.x + this.y * this.y + this.z * this.z;
  }
  length() {
    return Math.sqrt(this.x * this.x + this.y * this.y + this.z * this.z);
  }
  manhattanLength() {
    return Math.abs(this.x) + Math.abs(this.y) + Math.abs(this.z);
  }
  normalize() {
    return this.divideScalar(this.length() || 1);
  }
  setLength(e) {
    return this.normalize().multiplyScalar(e);
  }
  lerp(e, t) {
    return this.x += (e.x - this.x) * t, this.y += (e.y - this.y) * t, this.z += (e.z - this.z) * t, this;
  }
  lerpVectors(e, t, i) {
    return this.x = e.x + (t.x - e.x) * i, this.y = e.y + (t.y - e.y) * i, this.z = e.z + (t.z - e.z) * i, this;
  }
  cross(e) {
    return this.crossVectors(this, e);
  }
  crossVectors(e, t) {
    const i = e.x, n = e.y, s = e.z, a = t.x, o = t.y, l = t.z;
    return this.x = n * l - s * o, this.y = s * a - i * l, this.z = i * o - n * a, this;
  }
  projectOnVector(e) {
    const t = e.lengthSq();
    if (t === 0) return this.set(0, 0, 0);
    const i = e.dot(this) / t;
    return this.copy(e).multiplyScalar(i);
  }
  projectOnPlane(e) {
    return Da.copy(this).projectOnVector(e), this.sub(Da);
  }
  reflect(e) {
    return this.sub(Da.copy(e).multiplyScalar(2 * this.dot(e)));
  }
  angleTo(e) {
    const t = Math.sqrt(this.lengthSq() * e.lengthSq());
    if (t === 0) return Math.PI / 2;
    const i = this.dot(e) / t;
    return Math.acos($e(i, -1, 1));
  }
  distanceTo(e) {
    return Math.sqrt(this.distanceToSquared(e));
  }
  distanceToSquared(e) {
    const t = this.x - e.x, i = this.y - e.y, n = this.z - e.z;
    return t * t + i * i + n * n;
  }
  manhattanDistanceTo(e) {
    return Math.abs(this.x - e.x) + Math.abs(this.y - e.y) + Math.abs(this.z - e.z);
  }
  setFromSpherical(e) {
    return this.setFromSphericalCoords(e.radius, e.phi, e.theta);
  }
  setFromSphericalCoords(e, t, i) {
    const n = Math.sin(t) * e;
    return this.x = n * Math.sin(i), this.y = Math.cos(t) * e, this.z = n * Math.cos(i), this;
  }
  setFromCylindrical(e) {
    return this.setFromCylindricalCoords(e.radius, e.theta, e.y);
  }
  setFromCylindricalCoords(e, t, i) {
    return this.x = e * Math.sin(t), this.y = i, this.z = e * Math.cos(t), this;
  }
  setFromMatrixPosition(e) {
    const t = e.elements;
    return this.x = t[12], this.y = t[13], this.z = t[14], this;
  }
  setFromMatrixScale(e) {
    const t = this.setFromMatrixColumn(e, 0).length(), i = this.setFromMatrixColumn(e, 1).length(), n = this.setFromMatrixColumn(e, 2).length();
    return this.x = t, this.y = i, this.z = n, this;
  }
  setFromMatrixColumn(e, t) {
    return this.fromArray(e.elements, t * 4);
  }
  setFromMatrix3Column(e, t) {
    return this.fromArray(e.elements, t * 3);
  }
  setFromEuler(e) {
    return this.x = e._x, this.y = e._y, this.z = e._z, this;
  }
  setFromColor(e) {
    return this.x = e.r, this.y = e.g, this.z = e.b, this;
  }
  equals(e) {
    return e.x === this.x && e.y === this.y && e.z === this.z;
  }
  fromArray(e, t = 0) {
    return this.x = e[t], this.y = e[t + 1], this.z = e[t + 2], this;
  }
  toArray(e = [], t = 0) {
    return e[t] = this.x, e[t + 1] = this.y, e[t + 2] = this.z, e;
  }
  fromBufferAttribute(e, t) {
    return this.x = e.getX(t), this.y = e.getY(t), this.z = e.getZ(t), this;
  }
  random() {
    return this.x = Math.random(), this.y = Math.random(), this.z = Math.random(), this;
  }
  randomDirection() {
    const e = Math.random() * Math.PI * 2, t = Math.random() * 2 - 1, i = Math.sqrt(1 - t * t);
    return this.x = i * Math.cos(e), this.y = t, this.z = i * Math.sin(e), this;
  }
  *[Symbol.iterator]() {
    yield this.x, yield this.y, yield this.z;
  }
};
Gu.prototype.isVector3 = !0;
let P = Gu;
const Da = new P(), _c = new Ce(), Wu = class {
  constructor(e, t, i, n, s, a, o, l, c) {
    this.elements = [1, 0, 0, 0, 1, 0, 0, 0, 1], e !== void 0 && this.set(e, t, i, n, s, a, o, l, c);
  }
  set(e, t, i, n, s, a, o, l, c) {
    const h = this.elements;
    return h[0] = e, h[1] = n, h[2] = o, h[3] = t, h[4] = s, h[5] = l, h[6] = i, h[7] = a, h[8] = c, this;
  }
  identity() {
    return this.set(1, 0, 0, 0, 1, 0, 0, 0, 1), this;
  }
  copy(e) {
    const t = this.elements, i = e.elements;
    return t[0] = i[0], t[1] = i[1], t[2] = i[2], t[3] = i[3], t[4] = i[4], t[5] = i[5], t[6] = i[6], t[7] = i[7], t[8] = i[8], this;
  }
  extractBasis(e, t, i) {
    return e.setFromMatrix3Column(this, 0), t.setFromMatrix3Column(this, 1), i.setFromMatrix3Column(this, 2), this;
  }
  setFromMatrix4(e) {
    const t = e.elements;
    return this.set(t[0], t[4], t[8], t[1], t[5], t[9], t[2], t[6], t[10]), this;
  }
  multiply(e) {
    return this.multiplyMatrices(this, e);
  }
  premultiply(e) {
    return this.multiplyMatrices(e, this);
  }
  multiplyMatrices(e, t) {
    const i = e.elements, n = t.elements, s = this.elements, a = i[0], o = i[3], l = i[6], c = i[1], h = i[4], d = i[7], u = i[2], p = i[5], f = i[8], v = n[0], m = n[3], g = n[6], E = n[1], R = n[4], y = n[7], S = n[2], T = n[5], A = n[8];
    return s[0] = a * v + o * E + l * S, s[3] = a * m + o * R + l * T, s[6] = a * g + o * y + l * A, s[1] = c * v + h * E + d * S, s[4] = c * m + h * R + d * T, s[7] = c * g + h * y + d * A, s[2] = u * v + p * E + f * S, s[5] = u * m + p * R + f * T, s[8] = u * g + p * y + f * A, this;
  }
  multiplyScalar(e) {
    const t = this.elements;
    return t[0] *= e, t[3] *= e, t[6] *= e, t[1] *= e, t[4] *= e, t[7] *= e, t[2] *= e, t[5] *= e, t[8] *= e, this;
  }
  determinant() {
    const e = this.elements, t = e[0], i = e[1], n = e[2], s = e[3], a = e[4], o = e[5], l = e[6], c = e[7], h = e[8];
    return t * a * h - t * o * c - i * s * h + i * o * l + n * s * c - n * a * l;
  }
  invert() {
    const e = this.elements, t = e[0], i = e[1], n = e[2], s = e[3], a = e[4], o = e[5], l = e[6], c = e[7], h = e[8], d = h * a - o * c, u = o * l - h * s, p = c * s - a * l, f = t * d + i * u + n * p;
    if (f === 0) return this.set(0, 0, 0, 0, 0, 0, 0, 0, 0);
    const v = 1 / f;
    return e[0] = d * v, e[1] = (n * c - h * i) * v, e[2] = (o * i - n * a) * v, e[3] = u * v, e[4] = (h * t - n * l) * v, e[5] = (n * s - o * t) * v, e[6] = p * v, e[7] = (i * l - c * t) * v, e[8] = (a * t - i * s) * v, this;
  }
  transpose() {
    let e;
    const t = this.elements;
    return e = t[1], t[1] = t[3], t[3] = e, e = t[2], t[2] = t[6], t[6] = e, e = t[5], t[5] = t[7], t[7] = e, this;
  }
  getNormalMatrix(e) {
    return this.setFromMatrix4(e).invert().transpose();
  }
  transposeIntoArray(e) {
    const t = this.elements;
    return e[0] = t[0], e[1] = t[3], e[2] = t[6], e[3] = t[1], e[4] = t[4], e[5] = t[7], e[6] = t[2], e[7] = t[5], e[8] = t[8], this;
  }
  setUvTransform(e, t, i, n, s, a, o) {
    const l = Math.cos(s), c = Math.sin(s);
    return this.set(i * l, i * c, -i * (l * a + c * o) + a + e, -n * c, n * l, -n * (-c * a + l * o) + o + t, 0, 0, 1), this;
  }
  scale(e, t) {
    return pn("Matrix3: .scale() is deprecated. Use .makeScale() instead."), this.premultiply(Oa.makeScale(e, t)), this;
  }
  rotate(e) {
    return pn("Matrix3: .rotate() is deprecated. Use .makeRotation() instead."), this.premultiply(Oa.makeRotation(-e)), this;
  }
  translate(e, t) {
    return pn("Matrix3: .translate() is deprecated. Use .makeTranslation() instead."), this.premultiply(Oa.makeTranslation(e, t)), this;
  }
  makeTranslation(e, t) {
    return e.isVector2 ? this.set(1, 0, e.x, 0, 1, e.y, 0, 0, 1) : this.set(1, 0, e, 0, 1, t, 0, 0, 1), this;
  }
  makeRotation(e) {
    const t = Math.cos(e), i = Math.sin(e);
    return this.set(t, -i, 0, i, t, 0, 0, 0, 1), this;
  }
  makeScale(e, t) {
    return this.set(e, 0, 0, 0, t, 0, 0, 0, 1), this;
  }
  equals(e) {
    const t = this.elements, i = e.elements;
    for (let n = 0; n < 9; n++) if (t[n] !== i[n]) return !1;
    return !0;
  }
  fromArray(e, t = 0) {
    for (let i = 0; i < 9; i++) this.elements[i] = e[i + t];
    return this;
  }
  toArray(e = [], t = 0) {
    const i = this.elements;
    return e[t] = i[0], e[t + 1] = i[1], e[t + 2] = i[2], e[t + 3] = i[3], e[t + 4] = i[4], e[t + 5] = i[5], e[t + 6] = i[6], e[t + 7] = i[7], e[t + 8] = i[8], e;
  }
  clone() {
    return new this.constructor().fromArray(this.elements);
  }
};
Wu.prototype.isMatrix3 = !0;
let De = Wu;
const Oa = new De(), vc = new De().set(0.4123908, 0.3575843, 0.1804808, 0.212639, 0.7151687, 0.0721923, 0.0193308, 0.1191948, 0.9505322), xc = new De().set(3.2409699, -1.5373832, -0.4986108, -0.9692436, 1.8759675, 0.0415551, 0.0556301, -0.203977, 1.0569715);
function pf() {
  const r = { enabled: !0, workingColorSpace: ei, spaces: {}, convert: function(n, s, a) {
    return this.enabled === !1 || s === a || !s || !a || (this.spaces[s].transfer === ot && (n.r = Qi(n.r), n.g = Qi(n.g), n.b = Qi(n.b)), this.spaces[s].primaries !== this.spaces[a].primaries && (n.applyMatrix3(this.spaces[s].toXYZ), n.applyMatrix3(this.spaces[a].fromXYZ)), this.spaces[a].transfer === ot && (n.r = fn(n.r), n.g = fn(n.g), n.b = fn(n.b))), n;
  }, workingToColorSpace: function(n, s) {
    return this.convert(n, this.workingColorSpace, s);
  }, colorSpaceToWorking: function(n, s) {
    return this.convert(n, s, this.workingColorSpace);
  }, getPrimaries: function(n) {
    return this.spaces[n].primaries;
  }, getTransfer: function(n) {
    return n === pr ? ca : this.spaces[n].transfer;
  }, getToneMappingMode: function(n) {
    return this.spaces[n].outputColorSpaceConfig.toneMappingMode || "standard";
  }, getLuminanceCoefficients: function(n, s = this.workingColorSpace) {
    return n.fromArray(this.spaces[s].luminanceCoefficients);
  }, define: function(n) {
    Object.assign(this.spaces, n);
  }, _getMatrix: function(n, s, a) {
    return n.copy(this.spaces[s].toXYZ).multiply(this.spaces[a].fromXYZ);
  }, _getDrawingBufferColorSpace: function(n) {
    return this.spaces[n].outputColorSpaceConfig.drawingBufferColorSpace;
  }, _getUnpackColorSpace: function(n = this.workingColorSpace) {
    return this.spaces[n].workingColorSpaceConfig.unpackColorSpace;
  }, fromWorkingColorSpace: function(n, s) {
    return pn("ColorManagement: .fromWorkingColorSpace() has been renamed to .workingToColorSpace()."), r.workingToColorSpace(n, s);
  }, toWorkingColorSpace: function(n, s) {
    return pn("ColorManagement: .toWorkingColorSpace() has been renamed to .colorSpaceToWorking()."), r.colorSpaceToWorking(n, s);
  } }, e = [0.64, 0.33, 0.3, 0.6, 0.15, 0.06], t = [0.2126, 0.7152, 0.0722], i = [0.3127, 0.329];
  return r.define({ [ei]: { primaries: e, whitePoint: i, transfer: ca, toXYZ: vc, fromXYZ: xc, luminanceCoefficients: t, workingColorSpaceConfig: { unpackColorSpace: Ot }, outputColorSpaceConfig: { drawingBufferColorSpace: Ot } }, [Ot]: { primaries: e, whitePoint: i, transfer: ot, toXYZ: vc, fromXYZ: xc, luminanceCoefficients: t, outputColorSpaceConfig: { drawingBufferColorSpace: Ot } } }), r;
}
const Ze = pf();
function Qi(r) {
  return r < 0.04045 ? r * 0.0773993808 : Math.pow(r * 0.9478672986 + 0.0521327014, 2.4);
}
function fn(r) {
  return r < 31308e-7 ? r * 12.92 : 1.055 * Math.pow(r, 0.41666) - 0.055;
}
let Br;
class ff {
  static getDataURL(e, t = "image/png") {
    if (/^data:/i.test(e.src) || typeof HTMLCanvasElement > "u") return e.src;
    let i;
    if (e instanceof HTMLCanvasElement) i = e;
    else {
      Br === void 0 && (Br = ls("canvas")), Br.width = e.width, Br.height = e.height;
      const n = Br.getContext("2d");
      e instanceof ImageData ? n.putImageData(e, 0, 0) : n.drawImage(e, 0, 0, e.width, e.height), i = Br;
    }
    return i.toDataURL(t);
  }
  static sRGBToLinear(e) {
    if (typeof HTMLImageElement < "u" && e instanceof HTMLImageElement || typeof HTMLCanvasElement < "u" && e instanceof HTMLCanvasElement || typeof ImageBitmap < "u" && e instanceof ImageBitmap) {
      const t = ls("canvas");
      t.width = e.width, t.height = e.height;
      const i = t.getContext("2d");
      i.drawImage(e, 0, 0, e.width, e.height);
      const n = i.getImageData(0, 0, e.width, e.height), s = n.data;
      for (let a = 0; a < s.length; a++) s[a] = Qi(s[a] / 255) * 255;
      return i.putImageData(n, 0, 0), t;
    } else if (e.data) {
      const t = e.data.slice(0);
      for (let i = 0; i < t.length; i++) t instanceof Uint8Array || t instanceof Uint8ClampedArray ? t[i] = Math.floor(Qi(t[i] / 255) * 255) : t[i] = Qi(t[i]);
      return { data: t, width: e.width, height: e.height };
    } else return Re("ImageUtils.sRGBToLinear(): Unsupported image type. No color space conversion applied."), e;
  }
}
let mf = 0;
class jl {
  constructor(e = null) {
    this.isTextureSource = !0, Object.defineProperty(this, "id", { value: mf++ }), this.uuid = gi(), this.data = e, this.dataReady = !0, this.version = 0;
  }
  getSize(e) {
    const t = this.data;
    return typeof HTMLVideoElement < "u" && t instanceof HTMLVideoElement ? e.set(t.videoWidth, t.videoHeight, 0) : typeof VideoFrame < "u" && t instanceof VideoFrame ? e.set(t.displayWidth, t.displayHeight, 0) : t !== null ? e.set(t.width, t.height, t.depth || 0) : e.set(0, 0, 0), e;
  }
  set needsUpdate(e) {
    e === !0 && this.version++;
  }
  toJSON(e) {
    const t = e === void 0 || typeof e == "string";
    if (!t && e.images[this.uuid] !== void 0) return e.images[this.uuid];
    const i = { uuid: this.uuid, url: "" }, n = this.data;
    if (n !== null) {
      let s;
      if (Array.isArray(n)) {
        s = [];
        for (let a = 0, o = n.length; a < o; a++) n[a].isDataTexture ? s.push(Fa(n[a].image)) : s.push(Fa(n[a]));
      } else s = Fa(n);
      i.url = s;
    }
    return t || (e.images[this.uuid] = i), i;
  }
}
function Fa(r) {
  return typeof HTMLImageElement < "u" && r instanceof HTMLImageElement || typeof HTMLCanvasElement < "u" && r instanceof HTMLCanvasElement || typeof ImageBitmap < "u" && r instanceof ImageBitmap ? ff.getDataURL(r) : r.data ? { data: Array.from(r.data), width: r.width, height: r.height, type: r.data.constructor.name } : (Re("Texture: Unable to serialize Texture."), {});
}
let gf = 0;
const Ba = new P();
class Nt extends mr {
  constructor(e = Nt.DEFAULT_IMAGE, t = Nt.DEFAULT_MAPPING, i = Ci, n = Ci, s = Pt, a = Yi, o = li, l = $t, c = Nt.DEFAULT_ANISOTROPY, h = pr) {
    super(), this.isTexture = !0, Object.defineProperty(this, "id", { value: gf++ }), this.uuid = gi(), this.name = "", this.source = new jl(e), this.mipmaps = [], this.mapping = t, this.channel = 0, this.wrapS = i, this.wrapT = n, this.magFilter = s, this.minFilter = a, this.anisotropy = c, this.format = o, this.internalFormat = null, this.type = l, this.offset = new We(0, 0), this.repeat = new We(1, 1), this.center = new We(0, 0), this.rotation = 0, this.matrixAutoUpdate = !0, this.matrix = new De(), this.generateMipmaps = !0, this.premultiplyAlpha = !1, this.flipY = !0, this.unpackAlignment = 4, this.colorSpace = h, this.userData = {}, this.updateRanges = [], this.version = 0, this.onUpdate = null, this.renderTarget = null, this.isRenderTargetTexture = !1, this.isArrayTexture = !!(e && e.depth && e.depth > 1), this.pmremVersion = 0, this.normalized = !1;
  }
  get width() {
    return this.source.getSize(Ba).x;
  }
  get height() {
    return this.source.getSize(Ba).y;
  }
  get depth() {
    return this.source.getSize(Ba).z;
  }
  get image() {
    return this.source.data;
  }
  set image(e) {
    this.source.data = e;
  }
  updateMatrix() {
    this.matrix.setUvTransform(this.offset.x, this.offset.y, this.repeat.x, this.repeat.y, this.rotation, this.center.x, this.center.y);
  }
  addUpdateRange(e, t) {
    this.updateRanges.push({ start: e, count: t });
  }
  clearUpdateRanges() {
    this.updateRanges.length = 0;
  }
  clone() {
    return new this.constructor().copy(this);
  }
  copy(e) {
    return this.name = e.name, this.source = e.source, this.mipmaps = e.mipmaps.slice(0), this.mapping = e.mapping, this.channel = e.channel, this.wrapS = e.wrapS, this.wrapT = e.wrapT, this.magFilter = e.magFilter, this.minFilter = e.minFilter, this.anisotropy = e.anisotropy, this.format = e.format, this.internalFormat = e.internalFormat, this.type = e.type, this.normalized = e.normalized, this.offset.copy(e.offset), this.repeat.copy(e.repeat), this.center.copy(e.center), this.rotation = e.rotation, this.matrixAutoUpdate = e.matrixAutoUpdate, this.matrix.copy(e.matrix), this.generateMipmaps = e.generateMipmaps, this.premultiplyAlpha = e.premultiplyAlpha, this.flipY = e.flipY, this.unpackAlignment = e.unpackAlignment, this.colorSpace = e.colorSpace, this.renderTarget = e.renderTarget, this.isRenderTargetTexture = e.isRenderTargetTexture, this.isArrayTexture = e.isArrayTexture, this.userData = JSON.parse(JSON.stringify(e.userData)), this.needsUpdate = !0, this;
  }
  setValues(e) {
    for (const t in e) {
      const i = e[t];
      if (i === void 0) {
        Re(`Texture.setValues(): parameter '${t}' has value of undefined.`);
        continue;
      }
      const n = this[t];
      if (n === void 0) {
        Re(`Texture.setValues(): property '${t}' does not exist.`);
        continue;
      }
      n && i && n.isVector2 && i.isVector2 || n && i && n.isVector3 && i.isVector3 || n && i && n.isMatrix3 && i.isMatrix3 ? n.copy(i) : this[t] = i;
    }
  }
  toJSON(e) {
    const t = e === void 0 || typeof e == "string";
    if (!t && e.textures[this.uuid] !== void 0) return e.textures[this.uuid];
    const i = { metadata: { version: 4.7, type: "Texture", generator: "Texture.toJSON" }, uuid: this.uuid, name: this.name, image: this.source.toJSON(e).uuid, mapping: this.mapping, channel: this.channel, repeat: [this.repeat.x, this.repeat.y], offset: [this.offset.x, this.offset.y], center: [this.center.x, this.center.y], rotation: this.rotation, wrap: [this.wrapS, this.wrapT], format: this.format, internalFormat: this.internalFormat, type: this.type, normalized: this.normalized, colorSpace: this.colorSpace, minFilter: this.minFilter, magFilter: this.magFilter, anisotropy: this.anisotropy, flipY: this.flipY, generateMipmaps: this.generateMipmaps, premultiplyAlpha: this.premultiplyAlpha, unpackAlignment: this.unpackAlignment };
    return Object.keys(this.userData).length > 0 && (i.userData = this.userData), t || (e.textures[this.uuid] = i), i;
  }
  dispose() {
    this.dispatchEvent({ type: "dispose" });
  }
  transformUv(e) {
    if (this.mapping !== Lu) return e;
    if (e.applyMatrix3(this.matrix), e.x < 0 || e.x > 1) switch (this.wrapS) {
      case _n:
        e.x = e.x - Math.floor(e.x);
        break;
      case Ci:
        e.x = e.x < 0 ? 0 : 1;
        break;
      case na:
        Math.abs(Math.floor(e.x) % 2) === 1 ? e.x = Math.ceil(e.x) - e.x : e.x = e.x - Math.floor(e.x);
        break;
    }
    if (e.y < 0 || e.y > 1) switch (this.wrapT) {
      case _n:
        e.y = e.y - Math.floor(e.y);
        break;
      case Ci:
        e.y = e.y < 0 ? 0 : 1;
        break;
      case na:
        Math.abs(Math.floor(e.y) % 2) === 1 ? e.y = Math.ceil(e.y) - e.y : e.y = e.y - Math.floor(e.y);
        break;
    }
    return this.flipY && (e.y = 1 - e.y), e;
  }
  set needsUpdate(e) {
    e === !0 && (this.version++, this.source.needsUpdate = !0);
  }
  set needsPMREMUpdate(e) {
    e === !0 && this.pmremVersion++;
  }
}
Nt.DEFAULT_IMAGE = null, Nt.DEFAULT_MAPPING = Lu, Nt.DEFAULT_ANISOTROPY = 1;
const Xu = class {
  constructor(e = 0, t = 0, i = 0, n = 1) {
    this.x = e, this.y = t, this.z = i, this.w = n;
  }
  get width() {
    return this.z;
  }
  set width(e) {
    this.z = e;
  }
  get height() {
    return this.w;
  }
  set height(e) {
    this.w = e;
  }
  set(e, t, i, n) {
    return this.x = e, this.y = t, this.z = i, this.w = n, this;
  }
  setScalar(e) {
    return this.x = e, this.y = e, this.z = e, this.w = e, this;
  }
  setX(e) {
    return this.x = e, this;
  }
  setY(e) {
    return this.y = e, this;
  }
  setZ(e) {
    return this.z = e, this;
  }
  setW(e) {
    return this.w = e, this;
  }
  setComponent(e, t) {
    switch (e) {
      case 0:
        this.x = t;
        break;
      case 1:
        this.y = t;
        break;
      case 2:
        this.z = t;
        break;
      case 3:
        this.w = t;
        break;
      default:
        throw new Error("THREE.Vector4: index is out of range: " + e);
    }
    return this;
  }
  getComponent(e) {
    switch (e) {
      case 0:
        return this.x;
      case 1:
        return this.y;
      case 2:
        return this.z;
      case 3:
        return this.w;
      default:
        throw new Error("THREE.Vector4: index is out of range: " + e);
    }
  }
  clone() {
    return new this.constructor(this.x, this.y, this.z, this.w);
  }
  copy(e) {
    return this.x = e.x, this.y = e.y, this.z = e.z, this.w = e.w !== void 0 ? e.w : 1, this;
  }
  add(e) {
    return this.x += e.x, this.y += e.y, this.z += e.z, this.w += e.w, this;
  }
  addScalar(e) {
    return this.x += e, this.y += e, this.z += e, this.w += e, this;
  }
  addVectors(e, t) {
    return this.x = e.x + t.x, this.y = e.y + t.y, this.z = e.z + t.z, this.w = e.w + t.w, this;
  }
  addScaledVector(e, t) {
    return this.x += e.x * t, this.y += e.y * t, this.z += e.z * t, this.w += e.w * t, this;
  }
  sub(e) {
    return this.x -= e.x, this.y -= e.y, this.z -= e.z, this.w -= e.w, this;
  }
  subScalar(e) {
    return this.x -= e, this.y -= e, this.z -= e, this.w -= e, this;
  }
  subVectors(e, t) {
    return this.x = e.x - t.x, this.y = e.y - t.y, this.z = e.z - t.z, this.w = e.w - t.w, this;
  }
  multiply(e) {
    return this.x *= e.x, this.y *= e.y, this.z *= e.z, this.w *= e.w, this;
  }
  multiplyScalar(e) {
    return this.x *= e, this.y *= e, this.z *= e, this.w *= e, this;
  }
  applyMatrix4(e) {
    const t = this.x, i = this.y, n = this.z, s = this.w, a = e.elements;
    return this.x = a[0] * t + a[4] * i + a[8] * n + a[12] * s, this.y = a[1] * t + a[5] * i + a[9] * n + a[13] * s, this.z = a[2] * t + a[6] * i + a[10] * n + a[14] * s, this.w = a[3] * t + a[7] * i + a[11] * n + a[15] * s, this;
  }
  divide(e) {
    return this.x /= e.x, this.y /= e.y, this.z /= e.z, this.w /= e.w, this;
  }
  divideScalar(e) {
    return this.multiplyScalar(1 / e);
  }
  setAxisAngleFromQuaternion(e) {
    this.w = 2 * Math.acos(e.w);
    const t = Math.sqrt(1 - e.w * e.w);
    return t < 1e-4 ? (this.x = 1, this.y = 0, this.z = 0) : (this.x = e.x / t, this.y = e.y / t, this.z = e.z / t), this;
  }
  setAxisAngleFromRotationMatrix(e) {
    let t, i, n, s;
    const a = e.elements, o = a[0], l = a[4], c = a[8], h = a[1], d = a[5], u = a[9], p = a[2], f = a[6], v = a[10];
    if (Math.abs(l - h) < 0.01 && Math.abs(c - p) < 0.01 && Math.abs(u - f) < 0.01) {
      if (Math.abs(l + h) < 0.1 && Math.abs(c + p) < 0.1 && Math.abs(u + f) < 0.1 && Math.abs(o + d + v - 3) < 0.1) return this.set(1, 0, 0, 0), this;
      t = Math.PI;
      const g = (o + 1) / 2, E = (d + 1) / 2, R = (v + 1) / 2, y = (l + h) / 4, S = (c + p) / 4, T = (u + f) / 4;
      return g > E && g > R ? g < 0.01 ? (i = 0, n = 0.707106781, s = 0.707106781) : (i = Math.sqrt(g), n = y / i, s = S / i) : E > R ? E < 0.01 ? (i = 0.707106781, n = 0, s = 0.707106781) : (n = Math.sqrt(E), i = y / n, s = T / n) : R < 0.01 ? (i = 0.707106781, n = 0.707106781, s = 0) : (s = Math.sqrt(R), i = S / s, n = T / s), this.set(i, n, s, t), this;
    }
    let m = Math.sqrt((f - u) * (f - u) + (c - p) * (c - p) + (h - l) * (h - l));
    return Math.abs(m) < 1e-3 && (m = 1), this.x = (f - u) / m, this.y = (c - p) / m, this.z = (h - l) / m, this.w = Math.acos((o + d + v - 1) / 2), this;
  }
  setFromMatrixPosition(e) {
    const t = e.elements;
    return this.x = t[12], this.y = t[13], this.z = t[14], this.w = t[15], this;
  }
  min(e) {
    return this.x = Math.min(this.x, e.x), this.y = Math.min(this.y, e.y), this.z = Math.min(this.z, e.z), this.w = Math.min(this.w, e.w), this;
  }
  max(e) {
    return this.x = Math.max(this.x, e.x), this.y = Math.max(this.y, e.y), this.z = Math.max(this.z, e.z), this.w = Math.max(this.w, e.w), this;
  }
  clamp(e, t) {
    return this.x = $e(this.x, e.x, t.x), this.y = $e(this.y, e.y, t.y), this.z = $e(this.z, e.z, t.z), this.w = $e(this.w, e.w, t.w), this;
  }
  clampScalar(e, t) {
    return this.x = $e(this.x, e, t), this.y = $e(this.y, e, t), this.z = $e(this.z, e, t), this.w = $e(this.w, e, t), this;
  }
  clampLength(e, t) {
    const i = this.length();
    return this.divideScalar(i || 1).multiplyScalar($e(i, e, t));
  }
  floor() {
    return this.x = Math.floor(this.x), this.y = Math.floor(this.y), this.z = Math.floor(this.z), this.w = Math.floor(this.w), this;
  }
  ceil() {
    return this.x = Math.ceil(this.x), this.y = Math.ceil(this.y), this.z = Math.ceil(this.z), this.w = Math.ceil(this.w), this;
  }
  round() {
    return this.x = Math.round(this.x), this.y = Math.round(this.y), this.z = Math.round(this.z), this.w = Math.round(this.w), this;
  }
  roundToZero() {
    return this.x = Math.trunc(this.x), this.y = Math.trunc(this.y), this.z = Math.trunc(this.z), this.w = Math.trunc(this.w), this;
  }
  negate() {
    return this.x = -this.x, this.y = -this.y, this.z = -this.z, this.w = -this.w, this;
  }
  dot(e) {
    return this.x * e.x + this.y * e.y + this.z * e.z + this.w * e.w;
  }
  lengthSq() {
    return this.x * this.x + this.y * this.y + this.z * this.z + this.w * this.w;
  }
  length() {
    return Math.sqrt(this.x * this.x + this.y * this.y + this.z * this.z + this.w * this.w);
  }
  manhattanLength() {
    return Math.abs(this.x) + Math.abs(this.y) + Math.abs(this.z) + Math.abs(this.w);
  }
  normalize() {
    return this.divideScalar(this.length() || 1);
  }
  setLength(e) {
    return this.normalize().multiplyScalar(e);
  }
  lerp(e, t) {
    return this.x += (e.x - this.x) * t, this.y += (e.y - this.y) * t, this.z += (e.z - this.z) * t, this.w += (e.w - this.w) * t, this;
  }
  lerpVectors(e, t, i) {
    return this.x = e.x + (t.x - e.x) * i, this.y = e.y + (t.y - e.y) * i, this.z = e.z + (t.z - e.z) * i, this.w = e.w + (t.w - e.w) * i, this;
  }
  equals(e) {
    return e.x === this.x && e.y === this.y && e.z === this.z && e.w === this.w;
  }
  fromArray(e, t = 0) {
    return this.x = e[t], this.y = e[t + 1], this.z = e[t + 2], this.w = e[t + 3], this;
  }
  toArray(e = [], t = 0) {
    return e[t] = this.x, e[t + 1] = this.y, e[t + 2] = this.z, e[t + 3] = this.w, e;
  }
  fromBufferAttribute(e, t) {
    return this.x = e.getX(t), this.y = e.getY(t), this.z = e.getZ(t), this.w = e.getW(t), this;
  }
  random() {
    return this.x = Math.random(), this.y = Math.random(), this.z = Math.random(), this.w = Math.random(), this;
  }
  *[Symbol.iterator]() {
    yield this.x, yield this.y, yield this.z, yield this.w;
  }
};
Xu.prototype.isVector4 = !0;
let gt = Xu;
class _f extends mr {
  constructor(e = 1, t = 1, i = {}) {
    super(), i = Object.assign({ generateMipmaps: !1, internalFormat: null, minFilter: Pt, depthBuffer: !0, stencilBuffer: !1, resolveColorBuffer: !0, resolveDepthBuffer: !0, resolveStencilBuffer: !0, storeMultisampledColorBuffer: !0, storeMultisampledDepthBuffer: !0, storeMultisampledStencilBuffer: !0, depthTexture: null, samples: 0, count: 1, depth: 1, multiview: !1, useArrayDepthTexture: !1 }, i), this.isRenderTarget = !0, this.width = e, this.height = t, this.depth = i.depth, this.scissor = new gt(0, 0, e, t), this.scissorTest = !1, this.viewport = new gt(0, 0, e, t), this.textures = [];
    const n = { width: e, height: t, depth: i.depth }, s = new Nt(n), a = i.count;
    for (let o = 0; o < a; o++) this.textures[o] = s.clone(), this.textures[o].isRenderTargetTexture = !0, this.textures[o].renderTarget = this;
    this._setTextureOptions(i), this.depthBuffer = i.depthBuffer, this.stencilBuffer = i.stencilBuffer, this.resolveColorBuffer = i.resolveColorBuffer, this.resolveDepthBuffer = i.resolveDepthBuffer, this.resolveStencilBuffer = i.resolveStencilBuffer, this.storeMultisampledColorBuffer = i.storeMultisampledColorBuffer, this.storeMultisampledDepthBuffer = i.storeMultisampledDepthBuffer, this.storeMultisampledStencilBuffer = i.storeMultisampledStencilBuffer, this._depthTexture = null, this.depthTexture = i.depthTexture, this.samples = i.samples, this.multiview = i.multiview, this.useArrayDepthTexture = i.useArrayDepthTexture;
  }
  _setTextureOptions(e = {}) {
    const t = { minFilter: Pt, generateMipmaps: !1, flipY: !1, internalFormat: null };
    e.mapping !== void 0 && (t.mapping = e.mapping), e.wrapS !== void 0 && (t.wrapS = e.wrapS), e.wrapT !== void 0 && (t.wrapT = e.wrapT), e.wrapR !== void 0 && (t.wrapR = e.wrapR), e.magFilter !== void 0 && (t.magFilter = e.magFilter), e.minFilter !== void 0 && (t.minFilter = e.minFilter), e.format !== void 0 && (t.format = e.format), e.type !== void 0 && (t.type = e.type), e.anisotropy !== void 0 && (t.anisotropy = e.anisotropy), e.colorSpace !== void 0 && (t.colorSpace = e.colorSpace), e.flipY !== void 0 && (t.flipY = e.flipY), e.generateMipmaps !== void 0 && (t.generateMipmaps = e.generateMipmaps), e.internalFormat !== void 0 && (t.internalFormat = e.internalFormat);
    for (let i = 0; i < this.textures.length; i++) this.textures[i].setValues(t);
  }
  get texture() {
    return this.textures[0];
  }
  set texture(e) {
    this.textures[0] = e;
  }
  set depthTexture(e) {
    this._depthTexture !== null && this._depthTexture.renderTarget === this && (this._depthTexture.renderTarget = null), e !== null && e.renderTarget === null && (e.renderTarget = this), this._depthTexture = e;
  }
  get depthTexture() {
    return this._depthTexture;
  }
  setSize(e, t, i = 1) {
    if (this.width !== e || this.height !== t || this.depth !== i) {
      this.width = e, this.height = t, this.depth = i;
      for (let n = 0, s = this.textures.length; n < s; n++) this.textures[n].image.width = e, this.textures[n].image.height = t, this.textures[n].image.depth = i, this.textures[n].isData3DTexture !== !0 && (this.textures[n].isArrayTexture = this.textures[n].image.depth > 1);
      this.dispose();
    }
    this.viewport.set(0, 0, e, t), this.scissor.set(0, 0, e, t);
  }
  clone() {
    return new this.constructor().copy(this);
  }
  copy(e) {
    this.width = e.width, this.height = e.height, this.depth = e.depth, this.scissor.copy(e.scissor), this.scissorTest = e.scissorTest, this.viewport.copy(e.viewport), this.textures.length = 0;
    for (let t = 0, i = e.textures.length; t < i; t++) {
      this.textures[t] = e.textures[t].clone(), this.textures[t].isRenderTargetTexture = !0, this.textures[t].renderTarget = this;
      const n = Object.assign({}, e.textures[t].image);
      this.textures[t].source = new jl(n);
    }
    if (this.depthBuffer = e.depthBuffer, this.stencilBuffer = e.stencilBuffer, this.resolveColorBuffer = e.resolveColorBuffer, this.resolveDepthBuffer = e.resolveDepthBuffer, this.resolveStencilBuffer = e.resolveStencilBuffer, this.storeMultisampledColorBuffer = e.storeMultisampledColorBuffer, this.storeMultisampledDepthBuffer = e.storeMultisampledDepthBuffer, this.storeMultisampledStencilBuffer = e.storeMultisampledStencilBuffer, e.depthTexture !== null) if (e.depthTexture.renderTarget === e) {
      const t = e.depthTexture.clone();
      t.renderTarget = null, this.depthTexture = t;
    } else this.depthTexture = e.depthTexture;
    return this.samples = e.samples, this.multiview = e.multiview, this.useArrayDepthTexture = e.useArrayDepthTexture, this;
  }
  dispose() {
    this.dispatchEvent({ type: "dispose" });
  }
}
class _i extends _f {
  constructor(e = 1, t = 1, i = {}) {
    super(e, t, i), this.isWebGLRenderTarget = !0;
  }
}
class ju extends Nt {
  constructor(e = null, t = 1, i = 1, n = 1) {
    super(null), this.isDataArrayTexture = !0, this.image = { data: e, width: t, height: i, depth: n }, this.magFilter = Rt, this.minFilter = Rt, this.wrapR = Ci, this.generateMipmaps = !1, this.flipY = !1, this.unpackAlignment = 1, this.layerUpdates = /* @__PURE__ */ new Set();
  }
  copy(e) {
    return super.copy(e), this.wrapR = e.wrapR, this;
  }
  addLayerUpdate(e) {
    this.layerUpdates.add(e);
  }
  clearLayerUpdates() {
    this.layerUpdates.clear();
  }
}
class vf extends Nt {
  constructor(e = null, t = 1, i = 1, n = 1) {
    super(null), this.isData3DTexture = !0, this.image = { data: e, width: t, height: i, depth: n }, this.magFilter = Rt, this.minFilter = Rt, this.wrapR = Ci, this.generateMipmaps = !1, this.flipY = !1, this.unpackAlignment = 1;
  }
  copy(e) {
    return super.copy(e), this.wrapR = e.wrapR, this;
  }
}
const qu = class Yu {
  constructor(e, t, i, n, s, a, o, l, c, h, d, u, p, f, v, m) {
    this.elements = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], e !== void 0 && this.set(e, t, i, n, s, a, o, l, c, h, d, u, p, f, v, m);
  }
  set(e, t, i, n, s, a, o, l, c, h, d, u, p, f, v, m) {
    const g = this.elements;
    return g[0] = e, g[4] = t, g[8] = i, g[12] = n, g[1] = s, g[5] = a, g[9] = o, g[13] = l, g[2] = c, g[6] = h, g[10] = d, g[14] = u, g[3] = p, g[7] = f, g[11] = v, g[15] = m, this;
  }
  identity() {
    return this.set(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1), this;
  }
  clone() {
    return new Yu().fromArray(this.elements);
  }
  copy(e) {
    const t = this.elements, i = e.elements;
    return t[0] = i[0], t[1] = i[1], t[2] = i[2], t[3] = i[3], t[4] = i[4], t[5] = i[5], t[6] = i[6], t[7] = i[7], t[8] = i[8], t[9] = i[9], t[10] = i[10], t[11] = i[11], t[12] = i[12], t[13] = i[13], t[14] = i[14], t[15] = i[15], this;
  }
  copyPosition(e) {
    const t = this.elements, i = e.elements;
    return t[12] = i[12], t[13] = i[13], t[14] = i[14], this;
  }
  setFromMatrix3(e) {
    const t = e.elements;
    return this.set(t[0], t[3], t[6], 0, t[1], t[4], t[7], 0, t[2], t[5], t[8], 0, 0, 0, 0, 1), this;
  }
  extractBasis(e, t, i) {
    return this.determinantAffine() === 0 ? (e.set(1, 0, 0), t.set(0, 1, 0), i.set(0, 0, 1), this) : (e.setFromMatrixColumn(this, 0), t.setFromMatrixColumn(this, 1), i.setFromMatrixColumn(this, 2), this);
  }
  makeBasis(e, t, i) {
    return this.set(e.x, t.x, i.x, 0, e.y, t.y, i.y, 0, e.z, t.z, i.z, 0, 0, 0, 0, 1), this;
  }
  extractRotation(e) {
    if (e.determinantAffine() === 0) return this.identity();
    const t = this.elements, i = e.elements, n = 1 / Vr.setFromMatrixColumn(e, 0).length(), s = 1 / Vr.setFromMatrixColumn(e, 1).length(), a = 1 / Vr.setFromMatrixColumn(e, 2).length();
    return t[0] = i[0] * n, t[1] = i[1] * n, t[2] = i[2] * n, t[3] = 0, t[4] = i[4] * s, t[5] = i[5] * s, t[6] = i[6] * s, t[7] = 0, t[8] = i[8] * a, t[9] = i[9] * a, t[10] = i[10] * a, t[11] = 0, t[12] = 0, t[13] = 0, t[14] = 0, t[15] = 1, this;
  }
  makeRotationFromEuler(e) {
    const t = this.elements, i = e.x, n = e.y, s = e.z, a = Math.cos(i), o = Math.sin(i), l = Math.cos(n), c = Math.sin(n), h = Math.cos(s), d = Math.sin(s);
    if (e.order === "XYZ") {
      const u = a * h, p = a * d, f = o * h, v = o * d;
      t[0] = l * h, t[4] = -l * d, t[8] = c, t[1] = p + f * c, t[5] = u - v * c, t[9] = -o * l, t[2] = v - u * c, t[6] = f + p * c, t[10] = a * l;
    } else if (e.order === "YXZ") {
      const u = l * h, p = l * d, f = c * h, v = c * d;
      t[0] = u + v * o, t[4] = f * o - p, t[8] = a * c, t[1] = a * d, t[5] = a * h, t[9] = -o, t[2] = p * o - f, t[6] = v + u * o, t[10] = a * l;
    } else if (e.order === "ZXY") {
      const u = l * h, p = l * d, f = c * h, v = c * d;
      t[0] = u - v * o, t[4] = -a * d, t[8] = f + p * o, t[1] = p + f * o, t[5] = a * h, t[9] = v - u * o, t[2] = -a * c, t[6] = o, t[10] = a * l;
    } else if (e.order === "ZYX") {
      const u = a * h, p = a * d, f = o * h, v = o * d;
      t[0] = l * h, t[4] = f * c - p, t[8] = u * c + v, t[1] = l * d, t[5] = v * c + u, t[9] = p * c - f, t[2] = -c, t[6] = o * l, t[10] = a * l;
    } else if (e.order === "YZX") {
      const u = a * l, p = a * c, f = o * l, v = o * c;
      t[0] = l * h, t[4] = v - u * d, t[8] = f * d + p, t[1] = d, t[5] = a * h, t[9] = -o * h, t[2] = -c * h, t[6] = p * d + f, t[10] = u - v * d;
    } else if (e.order === "XZY") {
      const u = a * l, p = a * c, f = o * l, v = o * c;
      t[0] = l * h, t[4] = -d, t[8] = c * h, t[1] = u * d + v, t[5] = a * h, t[9] = p * d - f, t[2] = f * d - p, t[6] = o * h, t[10] = v * d + u;
    }
    return t[3] = 0, t[7] = 0, t[11] = 0, t[12] = 0, t[13] = 0, t[14] = 0, t[15] = 1, this;
  }
  makeRotationFromQuaternion(e) {
    return this.compose(xf, e, Mf);
  }
  lookAt(e, t, i) {
    const n = this.elements;
    return Kt.subVectors(e, t), Kt.lengthSq() === 0 && (Kt.z = 1), Kt.normalize(), nr.crossVectors(i, Kt), nr.lengthSq() === 0 && (Math.abs(i.z) === 1 ? Kt.x += 1e-4 : Kt.z += 1e-4, Kt.normalize(), nr.crossVectors(i, Kt)), nr.normalize(), gs.crossVectors(Kt, nr), n[0] = nr.x, n[4] = gs.x, n[8] = Kt.x, n[1] = nr.y, n[5] = gs.y, n[9] = Kt.y, n[2] = nr.z, n[6] = gs.z, n[10] = Kt.z, this;
  }
  multiply(e) {
    return this.multiplyMatrices(this, e);
  }
  premultiply(e) {
    return this.multiplyMatrices(e, this);
  }
  multiplyMatrices(e, t) {
    const i = e.elements, n = t.elements, s = this.elements, a = i[0], o = i[4], l = i[8], c = i[12], h = i[1], d = i[5], u = i[9], p = i[13], f = i[2], v = i[6], m = i[10], g = i[14], E = i[3], R = i[7], y = i[11], S = i[15], T = n[0], A = n[4], x = n[8], b = n[12], N = n[1], O = n[5], k = n[9], q = n[13], B = n[2], G = n[6], Z = n[10], X = n[14], te = n[3], V = n[7], K = n[11], ee = n[15];
    return s[0] = a * T + o * N + l * B + c * te, s[4] = a * A + o * O + l * G + c * V, s[8] = a * x + o * k + l * Z + c * K, s[12] = a * b + o * q + l * X + c * ee, s[1] = h * T + d * N + u * B + p * te, s[5] = h * A + d * O + u * G + p * V, s[9] = h * x + d * k + u * Z + p * K, s[13] = h * b + d * q + u * X + p * ee, s[2] = f * T + v * N + m * B + g * te, s[6] = f * A + v * O + m * G + g * V, s[10] = f * x + v * k + m * Z + g * K, s[14] = f * b + v * q + m * X + g * ee, s[3] = E * T + R * N + y * B + S * te, s[7] = E * A + R * O + y * G + S * V, s[11] = E * x + R * k + y * Z + S * K, s[15] = E * b + R * q + y * X + S * ee, this;
  }
  multiplyScalar(e) {
    const t = this.elements;
    return t[0] *= e, t[4] *= e, t[8] *= e, t[12] *= e, t[1] *= e, t[5] *= e, t[9] *= e, t[13] *= e, t[2] *= e, t[6] *= e, t[10] *= e, t[14] *= e, t[3] *= e, t[7] *= e, t[11] *= e, t[15] *= e, this;
  }
  determinant() {
    const e = this.elements, t = e[0], i = e[4], n = e[8], s = e[12], a = e[1], o = e[5], l = e[9], c = e[13], h = e[2], d = e[6], u = e[10], p = e[14], f = e[3], v = e[7], m = e[11], g = e[15], E = l * p - c * u, R = o * p - c * d, y = o * u - l * d, S = a * p - c * h, T = a * u - l * h, A = a * d - o * h;
    return t * (v * E - m * R + g * y) - i * (f * E - m * S + g * T) + n * (f * R - v * S + g * A) - s * (f * y - v * T + m * A);
  }
  determinantAffine() {
    const e = this.elements, t = e[0], i = e[4], n = e[8], s = e[1], a = e[5], o = e[9], l = e[2], c = e[6], h = e[10];
    return t * (a * h - o * c) - i * (s * h - o * l) + n * (s * c - a * l);
  }
  transpose() {
    const e = this.elements;
    let t;
    return t = e[1], e[1] = e[4], e[4] = t, t = e[2], e[2] = e[8], e[8] = t, t = e[6], e[6] = e[9], e[9] = t, t = e[3], e[3] = e[12], e[12] = t, t = e[7], e[7] = e[13], e[13] = t, t = e[11], e[11] = e[14], e[14] = t, this;
  }
  setPosition(e, t, i) {
    const n = this.elements;
    return e.isVector3 ? (n[12] = e.x, n[13] = e.y, n[14] = e.z) : (n[12] = e, n[13] = t, n[14] = i), this;
  }
  invert() {
    const e = this.elements, t = e[0], i = e[1], n = e[2], s = e[3], a = e[4], o = e[5], l = e[6], c = e[7], h = e[8], d = e[9], u = e[10], p = e[11], f = e[12], v = e[13], m = e[14], g = e[15], E = t * o - i * a, R = t * l - n * a, y = t * c - s * a, S = i * l - n * o, T = i * c - s * o, A = n * c - s * l, x = h * v - d * f, b = h * m - u * f, N = h * g - p * f, O = d * m - u * v, k = d * g - p * v, q = u * g - p * m, B = E * q - R * k + y * O + S * N - T * b + A * x;
    if (B === 0) return this.set(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
    const G = 1 / B;
    return e[0] = (o * q - l * k + c * O) * G, e[1] = (n * k - i * q - s * O) * G, e[2] = (v * A - m * T + g * S) * G, e[3] = (u * T - d * A - p * S) * G, e[4] = (l * N - a * q - c * b) * G, e[5] = (t * q - n * N + s * b) * G, e[6] = (m * y - f * A - g * R) * G, e[7] = (h * A - u * y + p * R) * G, e[8] = (a * k - o * N + c * x) * G, e[9] = (i * N - t * k - s * x) * G, e[10] = (f * T - v * y + g * E) * G, e[11] = (d * y - h * T - p * E) * G, e[12] = (o * b - a * O - l * x) * G, e[13] = (t * O - i * b + n * x) * G, e[14] = (v * R - f * S - m * E) * G, e[15] = (h * S - d * R + u * E) * G, this;
  }
  scale(e) {
    const t = this.elements, i = e.x, n = e.y, s = e.z;
    return t[0] *= i, t[4] *= n, t[8] *= s, t[1] *= i, t[5] *= n, t[9] *= s, t[2] *= i, t[6] *= n, t[10] *= s, t[3] *= i, t[7] *= n, t[11] *= s, this;
  }
  getMaxScaleOnAxis() {
    const e = this.elements, t = e[0] * e[0] + e[1] * e[1] + e[2] * e[2], i = e[4] * e[4] + e[5] * e[5] + e[6] * e[6], n = e[8] * e[8] + e[9] * e[9] + e[10] * e[10];
    return Math.sqrt(Math.max(t, i, n));
  }
  makeTranslation(e, t, i) {
    return e.isVector3 ? this.set(1, 0, 0, e.x, 0, 1, 0, e.y, 0, 0, 1, e.z, 0, 0, 0, 1) : this.set(1, 0, 0, e, 0, 1, 0, t, 0, 0, 1, i, 0, 0, 0, 1), this;
  }
  makeRotationX(e) {
    const t = Math.cos(e), i = Math.sin(e);
    return this.set(1, 0, 0, 0, 0, t, -i, 0, 0, i, t, 0, 0, 0, 0, 1), this;
  }
  makeRotationY(e) {
    const t = Math.cos(e), i = Math.sin(e);
    return this.set(t, 0, i, 0, 0, 1, 0, 0, -i, 0, t, 0, 0, 0, 0, 1), this;
  }
  makeRotationZ(e) {
    const t = Math.cos(e), i = Math.sin(e);
    return this.set(t, -i, 0, 0, i, t, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1), this;
  }
  makeRotationAxis(e, t) {
    const i = Math.cos(t), n = Math.sin(t), s = 1 - i, a = e.x, o = e.y, l = e.z, c = s * a, h = s * o;
    return this.set(c * a + i, c * o - n * l, c * l + n * o, 0, c * o + n * l, h * o + i, h * l - n * a, 0, c * l - n * o, h * l + n * a, s * l * l + i, 0, 0, 0, 0, 1), this;
  }
  makeScale(e, t, i) {
    return this.set(e, 0, 0, 0, 0, t, 0, 0, 0, 0, i, 0, 0, 0, 0, 1), this;
  }
  makeShear(e, t, i, n, s, a) {
    return this.set(1, i, s, 0, e, 1, a, 0, t, n, 1, 0, 0, 0, 0, 1), this;
  }
  compose(e, t, i) {
    const n = this.elements, s = t._x, a = t._y, o = t._z, l = t._w, c = s + s, h = a + a, d = o + o, u = s * c, p = s * h, f = s * d, v = a * h, m = a * d, g = o * d, E = l * c, R = l * h, y = l * d, S = i.x, T = i.y, A = i.z;
    return n[0] = (1 - (v + g)) * S, n[1] = (p + y) * S, n[2] = (f - R) * S, n[3] = 0, n[4] = (p - y) * T, n[5] = (1 - (u + g)) * T, n[6] = (m + E) * T, n[7] = 0, n[8] = (f + R) * A, n[9] = (m - E) * A, n[10] = (1 - (u + v)) * A, n[11] = 0, n[12] = e.x, n[13] = e.y, n[14] = e.z, n[15] = 1, this;
  }
  decompose(e, t, i) {
    const n = this.elements;
    e.x = n[12], e.y = n[13], e.z = n[14];
    const s = this.determinantAffine();
    if (s === 0) return i.set(1, 1, 1), t.identity(), this;
    let a = Vr.set(n[0], n[1], n[2]).length();
    const o = Vr.set(n[4], n[5], n[6]).length(), l = Vr.set(n[8], n[9], n[10]).length();
    s < 0 && (a = -a), hi.copy(this);
    const c = 1 / a, h = 1 / o, d = 1 / l;
    return hi.elements[0] *= c, hi.elements[1] *= c, hi.elements[2] *= c, hi.elements[4] *= h, hi.elements[5] *= h, hi.elements[6] *= h, hi.elements[8] *= d, hi.elements[9] *= d, hi.elements[10] *= d, t.setFromRotationMatrix(hi), i.x = a, i.y = o, i.z = l, this;
  }
  makePerspective(e, t, i, n, s, a, o = Li, l = !1) {
    const c = this.elements, h = 2 * s / (t - e), d = 2 * s / (i - n), u = (t + e) / (t - e), p = (i + n) / (i - n);
    let f, v;
    if (l) f = s / (a - s), v = a * s / (a - s);
    else if (o === Li) f = -(a + s) / (a - s), v = -2 * a * s / (a - s);
    else if (o === os) f = -a / (a - s), v = -a * s / (a - s);
    else throw new Error("THREE.Matrix4.makePerspective(): Invalid coordinate system: " + o);
    return c[0] = h, c[4] = 0, c[8] = u, c[12] = 0, c[1] = 0, c[5] = d, c[9] = p, c[13] = 0, c[2] = 0, c[6] = 0, c[10] = f, c[14] = v, c[3] = 0, c[7] = 0, c[11] = -1, c[15] = 0, this;
  }
  makeOrthographic(e, t, i, n, s, a, o = Li, l = !1) {
    const c = this.elements, h = 2 / (t - e), d = 2 / (i - n), u = -(t + e) / (t - e), p = -(i + n) / (i - n);
    let f, v;
    if (l) f = 1 / (a - s), v = a / (a - s);
    else if (o === Li) f = -2 / (a - s), v = -(a + s) / (a - s);
    else if (o === os) f = -1 / (a - s), v = -s / (a - s);
    else throw new Error("THREE.Matrix4.makeOrthographic(): Invalid coordinate system: " + o);
    return c[0] = h, c[4] = 0, c[8] = 0, c[12] = u, c[1] = 0, c[5] = d, c[9] = 0, c[13] = p, c[2] = 0, c[6] = 0, c[10] = f, c[14] = v, c[3] = 0, c[7] = 0, c[11] = 0, c[15] = 1, this;
  }
  equals(e) {
    const t = this.elements, i = e.elements;
    for (let n = 0; n < 16; n++) if (t[n] !== i[n]) return !1;
    return !0;
  }
  fromArray(e, t = 0) {
    for (let i = 0; i < 16; i++) this.elements[i] = e[i + t];
    return this;
  }
  toArray(e = [], t = 0) {
    const i = this.elements;
    return e[t] = i[0], e[t + 1] = i[1], e[t + 2] = i[2], e[t + 3] = i[3], e[t + 4] = i[4], e[t + 5] = i[5], e[t + 6] = i[6], e[t + 7] = i[7], e[t + 8] = i[8], e[t + 9] = i[9], e[t + 10] = i[10], e[t + 11] = i[11], e[t + 12] = i[12], e[t + 13] = i[13], e[t + 14] = i[14], e[t + 15] = i[15], e;
  }
};
qu.prototype.isMatrix4 = !0;
let Fe = qu;
const Vr = new P(), hi = new Fe(), xf = new P(0, 0, 0), Mf = new P(1, 1, 1), nr = new P(), gs = new P(), Kt = new P(), Mc = new Fe(), yc = new Ce();
class Yt {
  constructor(e = 0, t = 0, i = 0, n = Yt.DEFAULT_ORDER) {
    this.isEuler = !0, this._x = e, this._y = t, this._z = i, this._order = n;
  }
  get x() {
    return this._x;
  }
  set x(e) {
    this._x = e, this._onChangeCallback();
  }
  get y() {
    return this._y;
  }
  set y(e) {
    this._y = e, this._onChangeCallback();
  }
  get z() {
    return this._z;
  }
  set z(e) {
    this._z = e, this._onChangeCallback();
  }
  get order() {
    return this._order;
  }
  set order(e) {
    this._order = e, this._onChangeCallback();
  }
  set(e, t, i, n = this._order) {
    return this._x = e, this._y = t, this._z = i, this._order = n, this._onChangeCallback(), this;
  }
  clone() {
    return new this.constructor(this._x, this._y, this._z, this._order);
  }
  copy(e) {
    return this._x = e._x, this._y = e._y, this._z = e._z, this._order = e._order, this._onChangeCallback(), this;
  }
  setFromRotationMatrix(e, t = this._order, i = !0) {
    const n = e.elements, s = n[0], a = n[4], o = n[8], l = n[1], c = n[5], h = n[9], d = n[2], u = n[6], p = n[10];
    switch (t) {
      case "XYZ":
        this._y = Math.asin($e(o, -1, 1)), Math.abs(o) < 0.9999999 ? (this._x = Math.atan2(-h, p), this._z = Math.atan2(-a, s)) : (this._x = Math.atan2(u, c), this._z = 0);
        break;
      case "YXZ":
        this._x = Math.asin(-$e(h, -1, 1)), Math.abs(h) < 0.9999999 ? (this._y = Math.atan2(o, p), this._z = Math.atan2(l, c)) : (this._y = Math.atan2(-d, s), this._z = 0);
        break;
      case "ZXY":
        this._x = Math.asin($e(u, -1, 1)), Math.abs(u) < 0.9999999 ? (this._y = Math.atan2(-d, p), this._z = Math.atan2(-a, c)) : (this._y = 0, this._z = Math.atan2(l, s));
        break;
      case "ZYX":
        this._y = Math.asin(-$e(d, -1, 1)), Math.abs(d) < 0.9999999 ? (this._x = Math.atan2(u, p), this._z = Math.atan2(l, s)) : (this._x = 0, this._z = Math.atan2(-a, c));
        break;
      case "YZX":
        this._z = Math.asin($e(l, -1, 1)), Math.abs(l) < 0.9999999 ? (this._x = Math.atan2(-h, c), this._y = Math.atan2(-d, s)) : (this._x = 0, this._y = Math.atan2(o, p));
        break;
      case "XZY":
        this._z = Math.asin(-$e(a, -1, 1)), Math.abs(a) < 0.9999999 ? (this._x = Math.atan2(u, c), this._y = Math.atan2(o, s)) : (this._x = Math.atan2(-h, p), this._y = 0);
        break;
      default:
        Re("Euler: .setFromRotationMatrix() encountered an unknown order: " + t);
    }
    return this._order = t, i === !0 && this._onChangeCallback(), this;
  }
  setFromQuaternion(e, t, i) {
    return Mc.makeRotationFromQuaternion(e), this.setFromRotationMatrix(Mc, t, i);
  }
  setFromVector3(e, t = this._order) {
    return this.set(e.x, e.y, e.z, t);
  }
  reorder(e) {
    return yc.setFromEuler(this), this.setFromQuaternion(yc, e);
  }
  equals(e) {
    return e._x === this._x && e._y === this._y && e._z === this._z && e._order === this._order;
  }
  fromArray(e) {
    return this._x = e[0], this._y = e[1], this._z = e[2], e[3] !== void 0 && (this._order = e[3]), this._onChangeCallback(), this;
  }
  toArray(e = [], t = 0) {
    return e[t] = this._x, e[t + 1] = this._y, e[t + 2] = this._z, e[t + 3] = this._order, e;
  }
  _onChange(e) {
    return this._onChangeCallback = e, this;
  }
  _onChangeCallback() {
  }
  *[Symbol.iterator]() {
    yield this._x, yield this._y, yield this._z, yield this._order;
  }
}
Yt.DEFAULT_ORDER = "XYZ";
class Ku {
  constructor() {
    this.mask = 1;
  }
  set(e) {
    this.mask = (1 << e | 0) >>> 0;
  }
  enable(e) {
    this.mask |= 1 << e | 0;
  }
  enableAll() {
    this.mask = -1;
  }
  toggle(e) {
    this.mask ^= 1 << e | 0;
  }
  disable(e) {
    this.mask &= ~(1 << e | 0);
  }
  disableAll() {
    this.mask = 0;
  }
  test(e) {
    return (this.mask & e.mask) !== 0;
  }
  isEnabled(e) {
    return (this.mask & (1 << e | 0)) !== 0;
  }
}
let yf = 0;
const Sc = new P(), Hr = new Ce(), ki = new Fe(), _s = new P(), Pn = new P(), Sf = new P(), Tf = new Ce(), Tc = new P(1, 0, 0), Ec = new P(0, 1, 0), bc = new P(0, 0, 1), wc = { type: "added" }, Ef = { type: "removed" }, kr = { type: "childadded", child: null }, Va = { type: "childremoved", child: null };
class at extends mr {
  constructor() {
    super(), this.isObject3D = !0, Object.defineProperty(this, "id", { value: yf++ }), this.uuid = gi(), this.name = "", this.type = "Object3D", this.parent = null, this.children = [], this.up = at.DEFAULT_UP.clone();
    const e = new P(), t = new Yt(), i = new Ce(), n = new P(1, 1, 1);
    function s() {
      i.setFromEuler(t, !1);
    }
    function a() {
      t.setFromQuaternion(i, void 0, !1);
    }
    t._onChange(s), i._onChange(a), Object.defineProperties(this, { position: { configurable: !0, enumerable: !0, value: e }, rotation: { configurable: !0, enumerable: !0, value: t }, quaternion: { configurable: !0, enumerable: !0, value: i }, scale: { configurable: !0, enumerable: !0, value: n }, modelViewMatrix: { value: new Fe() }, normalMatrix: { value: new De() } }), this.matrix = new Fe(), this.matrixWorld = new Fe(), this.matrixAutoUpdate = at.DEFAULT_MATRIX_AUTO_UPDATE, this.matrixWorldAutoUpdate = at.DEFAULT_MATRIX_WORLD_AUTO_UPDATE, this.matrixWorldNeedsUpdate = !1, this.layers = new Ku(), this.visible = !0, this.castShadow = !1, this.receiveShadow = !1, this.frustumCulled = !0, this.renderOrder = 0, this.animations = [], this.customDepthMaterial = void 0, this.customDistanceMaterial = void 0, this.static = !1, this.userData = {}, this.pivot = null;
  }
  onBeforeShadow() {
  }
  onAfterShadow() {
  }
  onBeforeRender() {
  }
  onAfterRender() {
  }
  applyMatrix4(e) {
    this.matrixAutoUpdate && this.updateMatrix(), this.matrix.premultiply(e), this.matrix.decompose(this.position, this.quaternion, this.scale);
  }
  applyQuaternion(e) {
    return this.quaternion.premultiply(e), this;
  }
  setRotationFromAxisAngle(e, t) {
    this.quaternion.setFromAxisAngle(e, t);
  }
  setRotationFromEuler(e) {
    this.quaternion.setFromEuler(e, !0);
  }
  setRotationFromMatrix(e) {
    this.quaternion.setFromRotationMatrix(e);
  }
  setRotationFromQuaternion(e) {
    this.quaternion.copy(e);
  }
  rotateOnAxis(e, t) {
    return Hr.setFromAxisAngle(e, t), this.quaternion.multiply(Hr), this;
  }
  rotateOnWorldAxis(e, t) {
    return Hr.setFromAxisAngle(e, t), this.quaternion.premultiply(Hr), this;
  }
  rotateX(e) {
    return this.rotateOnAxis(Tc, e);
  }
  rotateY(e) {
    return this.rotateOnAxis(Ec, e);
  }
  rotateZ(e) {
    return this.rotateOnAxis(bc, e);
  }
  translateOnAxis(e, t) {
    return Sc.copy(e).applyQuaternion(this.quaternion), this.position.add(Sc.multiplyScalar(t)), this;
  }
  translateX(e) {
    return this.translateOnAxis(Tc, e);
  }
  translateY(e) {
    return this.translateOnAxis(Ec, e);
  }
  translateZ(e) {
    return this.translateOnAxis(bc, e);
  }
  localToWorld(e) {
    return this.updateWorldMatrix(!0, !1), e.applyMatrix4(this.matrixWorld);
  }
  worldToLocal(e) {
    return this.updateWorldMatrix(!0, !1), e.applyMatrix4(ki.copy(this.matrixWorld).invert());
  }
  lookAt(e, t, i) {
    e.isVector3 ? _s.copy(e) : _s.set(e, t, i);
    const n = this.parent;
    this.updateWorldMatrix(!0, !1), Pn.setFromMatrixPosition(this.matrixWorld), this.isCamera || this.isLight ? ki.lookAt(Pn, _s, this.up) : ki.lookAt(_s, Pn, this.up), this.quaternion.setFromRotationMatrix(ki), n && (ki.extractRotation(n.matrixWorld), Hr.setFromRotationMatrix(ki), this.quaternion.premultiply(Hr.invert()));
  }
  add(e) {
    if (arguments.length > 1) {
      for (let t = 0; t < arguments.length; t++) this.add(arguments[t]);
      return this;
    }
    return e === this ? (Be("Object3D.add: object can't be added as a child of itself.", e), this) : (e && e.isObject3D ? (e.removeFromParent(), e.parent = this, this.children.push(e), e.dispatchEvent(wc), kr.child = e, this.dispatchEvent(kr), kr.child = null) : Be("Object3D.add: object not an instance of THREE.Object3D.", e), this);
  }
  remove(e) {
    if (arguments.length > 1) {
      for (let i = 0; i < arguments.length; i++) this.remove(arguments[i]);
      return this;
    }
    const t = this.children.indexOf(e);
    return t !== -1 && (e.parent = null, this.children.splice(t, 1), e.dispatchEvent(Ef), Va.child = e, this.dispatchEvent(Va), Va.child = null), this;
  }
  removeFromParent() {
    const e = this.parent;
    return e !== null && e.remove(this), this;
  }
  clear() {
    return this.remove(...this.children);
  }
  attach(e) {
    return this.updateWorldMatrix(!0, !1), ki.copy(this.matrixWorld).invert(), e.parent !== null && (e.parent.updateWorldMatrix(!0, !1), ki.multiply(e.parent.matrixWorld)), e.applyMatrix4(ki), e.removeFromParent(), e.parent = this, this.children.push(e), e.updateWorldMatrix(!1, !0), e.dispatchEvent(wc), kr.child = e, this.dispatchEvent(kr), kr.child = null, this;
  }
  getObjectById(e) {
    return this.getObjectByProperty("id", e);
  }
  getObjectByName(e) {
    return this.getObjectByProperty("name", e);
  }
  getObjectByProperty(e, t) {
    if (this[e] === t) return this;
    for (let i = 0, n = this.children.length; i < n; i++) {
      const s = this.children[i].getObjectByProperty(e, t);
      if (s !== void 0) return s;
    }
  }
  getObjectsByProperty(e, t, i = []) {
    this[e] === t && i.push(this);
    const n = this.children;
    for (let s = 0, a = n.length; s < a; s++) n[s].getObjectsByProperty(e, t, i);
    return i;
  }
  getWorldPosition(e) {
    return this.updateWorldMatrix(!0, !1), e.setFromMatrixPosition(this.matrixWorld);
  }
  getWorldQuaternion(e) {
    return this.updateWorldMatrix(!0, !1), this.matrixWorld.decompose(Pn, e, Sf), e;
  }
  getWorldScale(e) {
    return this.updateWorldMatrix(!0, !1), this.matrixWorld.decompose(Pn, Tf, e), e;
  }
  getWorldDirection(e) {
    this.updateWorldMatrix(!0, !1);
    const t = this.matrixWorld.elements;
    return e.set(t[8], t[9], t[10]).normalize();
  }
  raycast() {
  }
  intersectsFrustum() {
  }
  traverse(e) {
    e(this);
    const t = this.children;
    for (let i = 0, n = t.length; i < n; i++) t[i].traverse(e);
  }
  traverseVisible(e) {
    if (this.visible === !1) return;
    e(this);
    const t = this.children;
    for (let i = 0, n = t.length; i < n; i++) t[i].traverseVisible(e);
  }
  traverseAncestors(e) {
    const t = this.parent;
    t !== null && (e(t), t.traverseAncestors(e));
  }
  updateMatrix() {
    this.matrix.compose(this.position, this.quaternion, this.scale);
    const e = this.pivot;
    if (e !== null) {
      const t = e.x, i = e.y, n = e.z, s = this.matrix.elements;
      s[12] += t - s[0] * t - s[4] * i - s[8] * n, s[13] += i - s[1] * t - s[5] * i - s[9] * n, s[14] += n - s[2] * t - s[6] * i - s[10] * n;
    }
    this.matrixWorldNeedsUpdate = !0;
  }
  updateMatrixWorld(e) {
    this.matrixAutoUpdate && this.updateMatrix(), (this.matrixWorldNeedsUpdate || e) && (this.matrixWorldAutoUpdate === !0 && (this.parent === null ? this.matrixWorld.copy(this.matrix) : this.matrixWorld.multiplyMatrices(this.parent.matrixWorld, this.matrix)), this.matrixWorldNeedsUpdate = !1, e = !0);
    const t = this.children;
    for (let i = 0, n = t.length; i < n; i++) t[i].updateMatrixWorld(e);
  }
  updateWorldMatrix(e, t, i = !1) {
    const n = this.parent;
    if (e === !0 && n !== null && n.updateWorldMatrix(!0, !1), this.matrixAutoUpdate && this.updateMatrix(), (this.matrixWorldNeedsUpdate || i) && (this.matrixWorldAutoUpdate === !0 && (this.parent === null ? this.matrixWorld.copy(this.matrix) : this.matrixWorld.multiplyMatrices(this.parent.matrixWorld, this.matrix)), this.matrixWorldNeedsUpdate = !1, i = !0), t === !0) {
      const s = this.children;
      for (let a = 0, o = s.length; a < o; a++) s[a].updateWorldMatrix(!1, !0, i);
    }
  }
  toJSON(e) {
    const t = e === void 0 || typeof e == "string", i = {};
    t && (e = { geometries: {}, materials: {}, textures: {}, images: {}, shapes: {}, skeletons: {}, animations: {}, nodes: {} }, i.metadata = { version: 4.7, type: "Object", generator: "Object3D.toJSON" });
    const n = {};
    n.uuid = this.uuid, n.type = this.type, n.name = this.name, n.castShadow = this.castShadow, n.receiveShadow = this.receiveShadow, n.visible = this.visible, n.frustumCulled = this.frustumCulled, n.renderOrder = this.renderOrder, n.static = this.static, n.matrixAutoUpdate = this.matrixAutoUpdate, Object.keys(this.userData).length > 0 && (n.userData = this.userData), n.layers = this.layers.mask, n.matrix = this.matrix.toArray(), n.up = this.up.toArray(), this.pivot !== null && (n.pivot = this.pivot.toArray()), this.morphTargetDictionary !== void 0 && (n.morphTargetDictionary = Object.assign({}, this.morphTargetDictionary)), this.morphTargetInfluences !== void 0 && (n.morphTargetInfluences = this.morphTargetInfluences.slice()), this.isInstancedMesh && (n.type = "InstancedMesh", n.count = this.count, n.instanceMatrix = this.instanceMatrix.toJSON(), this.instanceColor !== null && (n.instanceColor = this.instanceColor.toJSON())), this.isBatchedMesh && (n.type = "BatchedMesh", n.perObjectFrustumCulled = this.perObjectFrustumCulled, n.sortObjects = this.sortObjects, n.drawRanges = this._drawRanges, n.reservedRanges = this._reservedRanges, n.geometryInfo = this._geometryInfo.map((o) => ({ ...o, boundingBox: o.boundingBox ? o.boundingBox.toJSON() : void 0, boundingSphere: o.boundingSphere ? o.boundingSphere.toJSON() : void 0 })), n.instanceInfo = this._instanceInfo.map((o) => ({ ...o })), n.availableInstanceIds = this._availableInstanceIds.slice(), n.availableGeometryIds = this._availableGeometryIds.slice(), n.nextIndexStart = this._nextIndexStart, n.nextVertexStart = this._nextVertexStart, n.geometryCount = this._geometryCount, n.maxInstanceCount = this._maxInstanceCount, n.maxVertexCount = this._maxVertexCount, n.maxIndexCount = this._maxIndexCount, n.geometryInitialized = this._geometryInitialized, n.matricesTexture = this._matricesTexture.toJSON(e), n.indirectTexture = this._indirectTexture.toJSON(e), this._colorsTexture !== null && (n.colorsTexture = this._colorsTexture.toJSON(e)), this.boundingSphere !== null && (n.boundingSphere = this.boundingSphere.toJSON()), this.boundingBox !== null && (n.boundingBox = this.boundingBox.toJSON()));
    function s(o, l) {
      return o[l.uuid] === void 0 && (o[l.uuid] = l.toJSON(e)), l.uuid;
    }
    if (this.isScene) this.background && (this.background.isColor ? n.background = this.background.toJSON() : this.background.isTexture && (n.background = this.background.toJSON(e).uuid)), this.environment && this.environment.isTexture && this.environment.isRenderTargetTexture !== !0 && (n.environment = this.environment.toJSON(e).uuid);
    else if (this.isMesh || this.isLine || this.isPoints) {
      n.geometry = s(e.geometries, this.geometry);
      const o = this.geometry.parameters;
      if (o !== void 0 && o.shapes !== void 0) {
        const l = o.shapes;
        if (Array.isArray(l)) for (let c = 0, h = l.length; c < h; c++) {
          const d = l[c];
          s(e.shapes, d);
        }
        else s(e.shapes, l);
      }
    }
    if (this.isSkinnedMesh && (n.bindMode = this.bindMode, n.bindMatrix = this.bindMatrix.toArray(), this.skeleton !== void 0 && (s(e.skeletons, this.skeleton), n.skeleton = this.skeleton.uuid)), this.material !== void 0) if (Array.isArray(this.material)) {
      const o = [];
      for (let l = 0, c = this.material.length; l < c; l++) o.push(s(e.materials, this.material[l]));
      n.material = o;
    } else n.material = s(e.materials, this.material);
    if (this.children.length > 0) {
      n.children = [];
      for (let o = 0; o < this.children.length; o++) n.children.push(this.children[o].toJSON(e).object);
    }
    if (this.animations.length > 0) {
      n.animations = [];
      for (let o = 0; o < this.animations.length; o++) {
        const l = this.animations[o];
        n.animations.push(s(e.animations, l));
      }
    }
    if (t) {
      const o = a(e.geometries), l = a(e.materials), c = a(e.textures), h = a(e.images), d = a(e.shapes), u = a(e.skeletons), p = a(e.animations), f = a(e.nodes);
      o.length > 0 && (i.geometries = o), l.length > 0 && (i.materials = l), c.length > 0 && (i.textures = c), h.length > 0 && (i.images = h), d.length > 0 && (i.shapes = d), u.length > 0 && (i.skeletons = u), p.length > 0 && (i.animations = p), f.length > 0 && (i.nodes = f);
    }
    return i.object = n, i;
    function a(o) {
      const l = [];
      for (const c in o) {
        const h = o[c];
        delete h.metadata, l.push(h);
      }
      return l;
    }
  }
  clone(e) {
    return new this.constructor().copy(this, e);
  }
  copy(e, t = !0) {
    if (this.name = e.name, this.up.copy(e.up), this.position.copy(e.position), this.rotation.order = e.rotation.order, this.quaternion.copy(e.quaternion), this.scale.copy(e.scale), this.pivot = e.pivot !== null ? e.pivot.clone() : null, this.matrix.copy(e.matrix), this.matrixWorld.copy(e.matrixWorld), this.matrixAutoUpdate = e.matrixAutoUpdate, this.matrixWorldAutoUpdate = e.matrixWorldAutoUpdate, this.matrixWorldNeedsUpdate = e.matrixWorldNeedsUpdate, this.layers.mask = e.layers.mask, this.visible = e.visible, this.castShadow = e.castShadow, this.receiveShadow = e.receiveShadow, this.frustumCulled = e.frustumCulled, this.renderOrder = e.renderOrder, this.static = e.static, this.animations = e.animations.slice(), this.userData = JSON.parse(JSON.stringify(e.userData)), t === !0) for (let i = 0; i < e.children.length; i++) {
      const n = e.children[i];
      this.add(n.clone());
    }
    return this;
  }
  dispose() {
    this.dispatchEvent({ type: "dispose" });
  }
}
at.DEFAULT_UP = new P(0, 1, 0), at.DEFAULT_MATRIX_AUTO_UPDATE = !0, at.DEFAULT_MATRIX_WORLD_AUTO_UPDATE = !0;
class Qt extends at {
  constructor() {
    super(), this.isGroup = !0, this.type = "Group";
  }
}
const bf = { type: "move" };
class Ha {
  constructor() {
    this._targetRay = null, this._grip = null, this._hand = null;
  }
  getHandSpace() {
    return this._hand === null && (this._hand = new Qt(), this._hand.matrixAutoUpdate = !1, this._hand.visible = !1, this._hand.joints = {}, this._hand.inputState = { pinching: !1 }), this._hand;
  }
  getTargetRaySpace() {
    return this._targetRay === null && (this._targetRay = new Qt(), this._targetRay.matrixAutoUpdate = !1, this._targetRay.visible = !1, this._targetRay.hasLinearVelocity = !1, this._targetRay.linearVelocity = new P(), this._targetRay.hasAngularVelocity = !1, this._targetRay.angularVelocity = new P()), this._targetRay;
  }
  getGripSpace() {
    return this._grip === null && (this._grip = new Qt(), this._grip.matrixAutoUpdate = !1, this._grip.visible = !1, this._grip.hasLinearVelocity = !1, this._grip.linearVelocity = new P(), this._grip.hasAngularVelocity = !1, this._grip.angularVelocity = new P(), this._grip.eventsEnabled = !1), this._grip;
  }
  dispatchEvent(e) {
    return this._targetRay !== null && this._targetRay.dispatchEvent(e), this._grip !== null && this._grip.dispatchEvent(e), this._hand !== null && this._hand.dispatchEvent(e), this;
  }
  connect(e) {
    if (e && e.hand) {
      const t = this._hand;
      if (t) for (const i of e.hand.values()) this._getHandJoint(t, i);
    }
    return this.dispatchEvent({ type: "connected", data: e }), this;
  }
  disconnect(e) {
    return this.dispatchEvent({ type: "disconnected", data: e }), this._targetRay !== null && (this._targetRay.visible = !1), this._grip !== null && (this._grip.visible = !1), this._hand !== null && (this._hand.visible = !1), this;
  }
  update(e, t, i) {
    let n = null, s = null, a = null;
    const o = this._targetRay, l = this._grip, c = this._hand;
    if (e && t.session.visibilityState !== "visible-blurred") {
      if (c && e.hand) {
        a = !0;
        for (const v of e.hand.values()) {
          const m = t.getJointPose(v, i), g = this._getHandJoint(c, v);
          m !== null && (g.matrix.fromArray(m.transform.matrix), g.matrix.decompose(g.position, g.rotation, g.scale), g.matrixWorldNeedsUpdate = !0, g.jointRadius = m.radius), g.visible = m !== null;
        }
        const h = c.joints["index-finger-tip"], d = c.joints["thumb-tip"], u = h.position.distanceTo(d.position), p = 0.02, f = 5e-3;
        c.inputState.pinching && u > p + f ? (c.inputState.pinching = !1, this.dispatchEvent({ type: "pinchend", handedness: e.handedness, target: this })) : !c.inputState.pinching && u <= p - f && (c.inputState.pinching = !0, this.dispatchEvent({ type: "pinchstart", handedness: e.handedness, target: this }));
      } else l !== null && e.gripSpace && (s = t.getPose(e.gripSpace, i), s !== null && (l.matrix.fromArray(s.transform.matrix), l.matrix.decompose(l.position, l.rotation, l.scale), l.matrixWorldNeedsUpdate = !0, s.linearVelocity ? (l.hasLinearVelocity = !0, l.linearVelocity.copy(s.linearVelocity)) : l.hasLinearVelocity = !1, s.angularVelocity ? (l.hasAngularVelocity = !0, l.angularVelocity.copy(s.angularVelocity)) : l.hasAngularVelocity = !1, l.eventsEnabled && l.dispatchEvent({ type: "gripUpdated", data: e, target: this })));
      o !== null && (n = t.getPose(e.targetRaySpace, i), n === null && s !== null && (n = s), n !== null && (o.matrix.fromArray(n.transform.matrix), o.matrix.decompose(o.position, o.rotation, o.scale), o.matrixWorldNeedsUpdate = !0, n.linearVelocity ? (o.hasLinearVelocity = !0, o.linearVelocity.copy(n.linearVelocity)) : o.hasLinearVelocity = !1, n.angularVelocity ? (o.hasAngularVelocity = !0, o.angularVelocity.copy(n.angularVelocity)) : o.hasAngularVelocity = !1, this.dispatchEvent(bf)));
    }
    return o !== null && (o.visible = n !== null), l !== null && (l.visible = s !== null), c !== null && (c.visible = a !== null), this;
  }
  _getHandJoint(e, t) {
    if (e.joints[t.jointName] === void 0) {
      const i = new Qt();
      i.matrixAutoUpdate = !1, i.visible = !1, e.joints[t.jointName] = i, e.add(i);
    }
    return e.joints[t.jointName];
  }
}
const Zu = { aliceblue: 15792383, antiquewhite: 16444375, aqua: 65535, aquamarine: 8388564, azure: 15794175, beige: 16119260, bisque: 16770244, black: 0, blanchedalmond: 16772045, blue: 255, blueviolet: 9055202, brown: 10824234, burlywood: 14596231, cadetblue: 6266528, chartreuse: 8388352, chocolate: 13789470, coral: 16744272, cornflowerblue: 6591981, cornsilk: 16775388, crimson: 14423100, cyan: 65535, darkblue: 139, darkcyan: 35723, darkgoldenrod: 12092939, darkgray: 11119017, darkgreen: 25600, darkgrey: 11119017, darkkhaki: 12433259, darkmagenta: 9109643, darkolivegreen: 5597999, darkorange: 16747520, darkorchid: 10040012, darkred: 9109504, darksalmon: 15308410, darkseagreen: 9419919, darkslateblue: 4734347, darkslategray: 3100495, darkslategrey: 3100495, darkturquoise: 52945, darkviolet: 9699539, deeppink: 16716947, deepskyblue: 49151, dimgray: 6908265, dimgrey: 6908265, dodgerblue: 2003199, firebrick: 11674146, floralwhite: 16775920, forestgreen: 2263842, fuchsia: 16711935, gainsboro: 14474460, ghostwhite: 16316671, gold: 16766720, goldenrod: 14329120, gray: 8421504, green: 32768, greenyellow: 11403055, grey: 8421504, honeydew: 15794160, hotpink: 16738740, indianred: 13458524, indigo: 4915330, ivory: 16777200, khaki: 15787660, lavender: 15132410, lavenderblush: 16773365, lawngreen: 8190976, lemonchiffon: 16775885, lightblue: 11393254, lightcoral: 15761536, lightcyan: 14745599, lightgoldenrodyellow: 16448210, lightgray: 13882323, lightgreen: 9498256, lightgrey: 13882323, lightpink: 16758465, lightsalmon: 16752762, lightseagreen: 2142890, lightskyblue: 8900346, lightslategray: 7833753, lightslategrey: 7833753, lightsteelblue: 11584734, lightyellow: 16777184, lime: 65280, limegreen: 3329330, linen: 16445670, magenta: 16711935, maroon: 8388608, mediumaquamarine: 6737322, mediumblue: 205, mediumorchid: 12211667, mediumpurple: 9662683, mediumseagreen: 3978097, mediumslateblue: 8087790, mediumspringgreen: 64154, mediumturquoise: 4772300, mediumvioletred: 13047173, midnightblue: 1644912, mintcream: 16121850, mistyrose: 16770273, moccasin: 16770229, navajowhite: 16768685, navy: 128, oldlace: 16643558, olive: 8421376, olivedrab: 7048739, orange: 16753920, orangered: 16729344, orchid: 14315734, palegoldenrod: 15657130, palegreen: 10025880, paleturquoise: 11529966, palevioletred: 14381203, papayawhip: 16773077, peachpuff: 16767673, peru: 13468991, pink: 16761035, plum: 14524637, powderblue: 11591910, purple: 8388736, rebeccapurple: 6697881, red: 16711680, rosybrown: 12357519, royalblue: 4286945, saddlebrown: 9127187, salmon: 16416882, sandybrown: 16032864, seagreen: 3050327, seashell: 16774638, sienna: 10506797, silver: 12632256, skyblue: 8900331, slateblue: 6970061, slategray: 7372944, slategrey: 7372944, snow: 16775930, springgreen: 65407, steelblue: 4620980, tan: 13808780, teal: 32896, thistle: 14204888, tomato: 16737095, turquoise: 4251856, violet: 15631086, wheat: 16113331, white: 16777215, whitesmoke: 16119285, yellow: 16776960, yellowgreen: 10145074 }, sr = { h: 0, s: 0, l: 0 }, vs = { h: 0, s: 0, l: 0 };
function ka(r, e, t) {
  return t < 0 && (t += 1), t > 1 && (t -= 1), t < 1 / 6 ? r + (e - r) * 6 * t : t < 1 / 2 ? e : t < 2 / 3 ? r + (e - r) * 6 * (2 / 3 - t) : r;
}
class be {
  constructor(e, t, i) {
    return this.isColor = !0, this.r = 1, this.g = 1, this.b = 1, this.set(e, t, i);
  }
  set(e, t, i) {
    if (t === void 0 && i === void 0) {
      const n = e;
      n && n.isColor ? this.copy(n) : typeof n == "number" ? this.setHex(n) : typeof n == "string" && this.setStyle(n);
    } else this.setRGB(e, t, i);
    return this;
  }
  setScalar(e) {
    return this.r = e, this.g = e, this.b = e, this;
  }
  setHex(e, t = Ot) {
    return e = Math.floor(e), this.r = (e >> 16 & 255) / 255, this.g = (e >> 8 & 255) / 255, this.b = (e & 255) / 255, Ze.colorSpaceToWorking(this, t), this;
  }
  setRGB(e, t, i, n = Ze.workingColorSpace) {
    return this.r = e, this.g = t, this.b = i, Ze.colorSpaceToWorking(this, n), this;
  }
  setHSL(e, t, i, n = Ze.workingColorSpace) {
    if (e = Xl(e, 1), t = $e(t, 0, 1), i = $e(i, 0, 1), t === 0) this.r = this.g = this.b = i;
    else {
      const s = i <= 0.5 ? i * (1 + t) : i + t - i * t, a = 2 * i - s;
      this.r = ka(a, s, e + 1 / 3), this.g = ka(a, s, e), this.b = ka(a, s, e - 1 / 3);
    }
    return Ze.colorSpaceToWorking(this, n), this;
  }
  setStyle(e, t = Ot) {
    function i(s) {
      s !== void 0 && parseFloat(s) < 1 && Re("Color: Alpha component of " + e + " will be ignored.");
    }
    let n;
    if (n = /^(\w+)\(([^\)]*)\)/.exec(e)) {
      let s;
      const a = n[1], o = n[2];
      switch (a) {
        case "rgb":
        case "rgba":
          if (s = /^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*(\d*\.?\d+)\s*)?$/.exec(o)) return i(s[4]), this.setRGB(Math.min(255, parseInt(s[1], 10)) / 255, Math.min(255, parseInt(s[2], 10)) / 255, Math.min(255, parseInt(s[3], 10)) / 255, t);
          if (s = /^\s*(\d+)\%\s*,\s*(\d+)\%\s*,\s*(\d+)\%\s*(?:,\s*(\d*\.?\d+)\s*)?$/.exec(o)) return i(s[4]), this.setRGB(Math.min(100, parseInt(s[1], 10)) / 100, Math.min(100, parseInt(s[2], 10)) / 100, Math.min(100, parseInt(s[3], 10)) / 100, t);
          break;
        case "hsl":
        case "hsla":
          if (s = /^\s*(\d*\.?\d+)\s*,\s*(\d*\.?\d+)\%\s*,\s*(\d*\.?\d+)\%\s*(?:,\s*(\d*\.?\d+)\s*)?$/.exec(o)) return i(s[4]), this.setHSL(parseFloat(s[1]) / 360, parseFloat(s[2]) / 100, parseFloat(s[3]) / 100, t);
          break;
        default:
          Re("Color: Unknown color model " + e);
      }
    } else if (n = /^\#([A-Fa-f\d]+)$/.exec(e)) {
      const s = n[1], a = s.length;
      if (a === 3) return this.setRGB(parseInt(s.charAt(0), 16) / 15, parseInt(s.charAt(1), 16) / 15, parseInt(s.charAt(2), 16) / 15, t);
      if (a === 6) return this.setHex(parseInt(s, 16), t);
      Re("Color: Invalid hex color " + e);
    } else if (e && e.length > 0) return this.setColorName(e, t);
    return this;
  }
  setColorName(e, t = Ot) {
    const i = Zu[e.toLowerCase()];
    return i !== void 0 ? this.setHex(i, t) : Re("Color: Unknown color " + e), this;
  }
  clone() {
    return new this.constructor(this.r, this.g, this.b);
  }
  copy(e) {
    return this.r = e.r, this.g = e.g, this.b = e.b, this;
  }
  copySRGBToLinear(e) {
    return this.r = Qi(e.r), this.g = Qi(e.g), this.b = Qi(e.b), this;
  }
  copyLinearToSRGB(e) {
    return this.r = fn(e.r), this.g = fn(e.g), this.b = fn(e.b), this;
  }
  convertSRGBToLinear() {
    return this.copySRGBToLinear(this), this;
  }
  convertLinearToSRGB() {
    return this.copyLinearToSRGB(this), this;
  }
  getHex(e = Ot) {
    return Ze.workingToColorSpace(Bt.copy(this), e), Math.round($e(Bt.r * 255, 0, 255)) * 65536 + Math.round($e(Bt.g * 255, 0, 255)) * 256 + Math.round($e(Bt.b * 255, 0, 255));
  }
  getHexString(e = Ot) {
    return ("000000" + this.getHex(e).toString(16)).slice(-6);
  }
  getHSL(e, t = Ze.workingColorSpace) {
    Ze.workingToColorSpace(Bt.copy(this), t);
    const i = Bt.r, n = Bt.g, s = Bt.b, a = Math.max(i, n, s), o = Math.min(i, n, s);
    let l, c;
    const h = (o + a) / 2;
    if (o === a) l = 0, c = 0;
    else {
      const d = a - o;
      switch (c = h <= 0.5 ? d / (a + o) : d / (2 - a - o), a) {
        case i:
          l = (n - s) / d + (n < s ? 6 : 0);
          break;
        case n:
          l = (s - i) / d + 2;
          break;
        case s:
          l = (i - n) / d + 4;
          break;
      }
      l /= 6;
    }
    return e.h = l, e.s = c, e.l = h, e;
  }
  getRGB(e, t = Ze.workingColorSpace) {
    return Ze.workingToColorSpace(Bt.copy(this), t), e.r = Bt.r, e.g = Bt.g, e.b = Bt.b, e;
  }
  getStyle(e = Ot) {
    Ze.workingToColorSpace(Bt.copy(this), e);
    const t = Bt.r, i = Bt.g, n = Bt.b;
    return e !== Ot ? `color(${e} ${t.toFixed(3)} ${i.toFixed(3)} ${n.toFixed(3)})` : `rgb(${Math.round(t * 255)},${Math.round(i * 255)},${Math.round(n * 255)})`;
  }
  offsetHSL(e, t, i) {
    return this.getHSL(sr), this.setHSL(sr.h + e, sr.s + t, sr.l + i);
  }
  add(e) {
    return this.r += e.r, this.g += e.g, this.b += e.b, this;
  }
  addColors(e, t) {
    return this.r = e.r + t.r, this.g = e.g + t.g, this.b = e.b + t.b, this;
  }
  addScalar(e) {
    return this.r += e, this.g += e, this.b += e, this;
  }
  sub(e) {
    return this.r = Math.max(0, this.r - e.r), this.g = Math.max(0, this.g - e.g), this.b = Math.max(0, this.b - e.b), this;
  }
  multiply(e) {
    return this.r *= e.r, this.g *= e.g, this.b *= e.b, this;
  }
  multiplyScalar(e) {
    return this.r *= e, this.g *= e, this.b *= e, this;
  }
  lerp(e, t) {
    return this.r += (e.r - this.r) * t, this.g += (e.g - this.g) * t, this.b += (e.b - this.b) * t, this;
  }
  lerpColors(e, t, i) {
    return this.r = e.r + (t.r - e.r) * i, this.g = e.g + (t.g - e.g) * i, this.b = e.b + (t.b - e.b) * i, this;
  }
  lerpHSL(e, t) {
    this.getHSL(sr), e.getHSL(vs);
    const i = es(sr.h, vs.h, t), n = es(sr.s, vs.s, t), s = es(sr.l, vs.l, t);
    return this.setHSL(i, n, s), this;
  }
  setFromVector3(e) {
    return this.r = e.x, this.g = e.y, this.b = e.z, this;
  }
  applyMatrix3(e) {
    const t = this.r, i = this.g, n = this.b, s = e.elements;
    return this.r = s[0] * t + s[3] * i + s[6] * n, this.g = s[1] * t + s[4] * i + s[7] * n, this.b = s[2] * t + s[5] * i + s[8] * n, this;
  }
  equals(e) {
    return e.r === this.r && e.g === this.g && e.b === this.b;
  }
  fromArray(e, t = 0) {
    return this.r = e[t], this.g = e[t + 1], this.b = e[t + 2], this;
  }
  toArray(e = [], t = 0) {
    return e[t] = this.r, e[t + 1] = this.g, e[t + 2] = this.b, e;
  }
  fromBufferAttribute(e, t) {
    return this.r = e.getX(t), this.g = e.getY(t), this.b = e.getZ(t), this;
  }
  toJSON() {
    return this.getHex();
  }
  *[Symbol.iterator]() {
    yield this.r, yield this.g, yield this.b;
  }
}
const Bt = new be();
be.NAMES = Zu;
class wf extends at {
  constructor() {
    super(), this.isScene = !0, this.type = "Scene", this.background = null, this.environment = null, this.fog = null, this.backgroundBlurriness = 0, this.backgroundIntensity = 1, this.backgroundRotation = new Yt(), this.environmentIntensity = 1, this.environmentRotation = new Yt(), this.overrideMaterial = null, typeof __THREE_DEVTOOLS__ < "u" && __THREE_DEVTOOLS__.dispatchEvent(new CustomEvent("observe", { detail: this }));
  }
  copy(e, t) {
    return super.copy(e, t), e.background !== null && (this.background = e.background.clone()), e.environment !== null && (this.environment = e.environment.clone()), e.fog !== null && (this.fog = e.fog.clone()), this.backgroundBlurriness = e.backgroundBlurriness, this.backgroundIntensity = e.backgroundIntensity, this.backgroundRotation.copy(e.backgroundRotation), this.environmentIntensity = e.environmentIntensity, this.environmentRotation.copy(e.environmentRotation), e.overrideMaterial !== null && (this.overrideMaterial = e.overrideMaterial.clone()), this.matrixAutoUpdate = e.matrixAutoUpdate, this;
  }
  toJSON(e) {
    const t = super.toJSON(e);
    return this.fog !== null && (t.object.fog = this.fog.toJSON()), t.object.backgroundBlurriness = this.backgroundBlurriness, t.object.backgroundIntensity = this.backgroundIntensity, t.object.backgroundRotation = this.backgroundRotation.toArray(), t.object.environmentIntensity = this.environmentIntensity, t.object.environmentRotation = this.environmentRotation.toArray(), t;
  }
}
const ui = new P(), zi = new P(), za = new P(), Gi = new P(), zr = new P(), Gr = new P(), Ac = new P(), Ga = new P(), Wa = new P(), Xa = new P(), ja = new gt(), qa = new gt(), Ya = new gt();
class fi {
  constructor(e = new P(), t = new P(), i = new P()) {
    this.a = e, this.b = t, this.c = i;
  }
  static getNormal(e, t, i, n) {
    n.subVectors(i, t), ui.subVectors(e, t), n.cross(ui);
    const s = n.lengthSq();
    return s > 0 ? n.multiplyScalar(1 / Math.sqrt(s)) : n.set(0, 0, 0);
  }
  static getBarycoord(e, t, i, n, s) {
    ui.subVectors(n, t), zi.subVectors(i, t), za.subVectors(e, t);
    const a = ui.dot(ui), o = ui.dot(zi), l = ui.dot(za), c = zi.dot(zi), h = zi.dot(za), d = a * c - o * o;
    if (d === 0) return s.set(0, 0, 0), null;
    const u = 1 / d, p = (c * l - o * h) * u, f = (a * h - o * l) * u;
    return s.set(1 - p - f, f, p);
  }
  static containsPoint(e, t, i, n) {
    return this.getBarycoord(e, t, i, n, Gi) === null ? !1 : Gi.x >= 0 && Gi.y >= 0 && Gi.x + Gi.y <= 1;
  }
  static getInterpolation(e, t, i, n, s, a, o, l) {
    return this.getBarycoord(e, t, i, n, Gi) === null ? (l.x = 0, l.y = 0, "z" in l && (l.z = 0), "w" in l && (l.w = 0), null) : (l.setScalar(0), l.addScaledVector(s, Gi.x), l.addScaledVector(a, Gi.y), l.addScaledVector(o, Gi.z), l);
  }
  static getInterpolatedAttribute(e, t, i, n, s, a) {
    return ja.setScalar(0), qa.setScalar(0), Ya.setScalar(0), ja.fromBufferAttribute(e, t), qa.fromBufferAttribute(e, i), Ya.fromBufferAttribute(e, n), a.setScalar(0), a.addScaledVector(ja, s.x), a.addScaledVector(qa, s.y), a.addScaledVector(Ya, s.z), a;
  }
  static isFrontFacing(e, t, i, n) {
    return ui.subVectors(i, t), zi.subVectors(e, t), ui.cross(zi).dot(n) < 0;
  }
  set(e, t, i) {
    return this.a.copy(e), this.b.copy(t), this.c.copy(i), this;
  }
  setFromPointsAndIndices(e, t, i, n) {
    return this.a.copy(e[t]), this.b.copy(e[i]), this.c.copy(e[n]), this;
  }
  setFromAttributeAndIndices(e, t, i, n) {
    return this.a.fromBufferAttribute(e, t), this.b.fromBufferAttribute(e, i), this.c.fromBufferAttribute(e, n), this;
  }
  clone() {
    return new this.constructor().copy(this);
  }
  copy(e) {
    return this.a.copy(e.a), this.b.copy(e.b), this.c.copy(e.c), this;
  }
  getArea() {
    return ui.subVectors(this.c, this.b), zi.subVectors(this.a, this.b), ui.cross(zi).length() * 0.5;
  }
  getMidpoint(e) {
    return e.addVectors(this.a, this.b).add(this.c).multiplyScalar(1 / 3);
  }
  getNormal(e) {
    return fi.getNormal(this.a, this.b, this.c, e);
  }
  getPlane(e) {
    return e.setFromCoplanarPoints(this.a, this.b, this.c);
  }
  getBarycoord(e, t) {
    return fi.getBarycoord(e, this.a, this.b, this.c, t);
  }
  getInterpolation(e, t, i, n, s) {
    return fi.getInterpolation(e, this.a, this.b, this.c, t, i, n, s);
  }
  containsPoint(e) {
    return fi.containsPoint(e, this.a, this.b, this.c);
  }
  isFrontFacing(e) {
    return fi.isFrontFacing(this.a, this.b, this.c, e);
  }
  intersectsBox(e) {
    return e.intersectsTriangle(this);
  }
  closestPointToPoint(e, t) {
    const i = this.a, n = this.b, s = this.c;
    let a, o;
    zr.subVectors(n, i), Gr.subVectors(s, i), Ga.subVectors(e, i);
    const l = zr.dot(Ga), c = Gr.dot(Ga);
    if (l <= 0 && c <= 0) return t.copy(i);
    Wa.subVectors(e, n);
    const h = zr.dot(Wa), d = Gr.dot(Wa);
    if (h >= 0 && d <= h) return t.copy(n);
    const u = l * d - h * c;
    if (u <= 0 && l >= 0 && h <= 0) return a = l / (l - h), t.copy(i).addScaledVector(zr, a);
    Xa.subVectors(e, s);
    const p = zr.dot(Xa), f = Gr.dot(Xa);
    if (f >= 0 && p <= f) return t.copy(s);
    const v = p * c - l * f;
    if (v <= 0 && c >= 0 && f <= 0) return o = c / (c - f), t.copy(i).addScaledVector(Gr, o);
    const m = h * f - p * d;
    if (m <= 0 && d - h >= 0 && p - f >= 0) return Ac.subVectors(s, n), o = (d - h) / (d - h + (p - f)), t.copy(n).addScaledVector(Ac, o);
    const g = 1 / (m + v + u);
    return a = v * g, o = u * g, t.copy(i).addScaledVector(zr, a).addScaledVector(Gr, o);
  }
  equals(e) {
    return e.a.equals(this.a) && e.b.equals(this.b) && e.c.equals(this.c);
  }
}
class er {
  constructor(e = new P(1 / 0, 1 / 0, 1 / 0), t = new P(-1 / 0, -1 / 0, -1 / 0)) {
    this.isBox3 = !0, this.min = e, this.max = t;
  }
  set(e, t) {
    return this.min.copy(e), this.max.copy(t), this;
  }
  setFromArray(e) {
    this.makeEmpty();
    for (let t = 0, i = e.length; t < i; t += 3) this.expandByPoint(di.fromArray(e, t));
    return this;
  }
  setFromBufferAttribute(e) {
    this.makeEmpty();
    for (let t = 0, i = e.count; t < i; t++) this.expandByPoint(di.fromBufferAttribute(e, t));
    return this;
  }
  setFromPoints(e) {
    this.makeEmpty();
    for (let t = 0, i = e.length; t < i; t++) this.expandByPoint(e[t]);
    return this;
  }
  setFromCenterAndSize(e, t) {
    const i = di.copy(t).multiplyScalar(0.5);
    return this.min.copy(e).sub(i), this.max.copy(e).add(i), this;
  }
  setFromObject(e, t = !1) {
    return this.makeEmpty(), this.expandByObject(e, t);
  }
  clone() {
    return new this.constructor().copy(this);
  }
  copy(e) {
    return this.min.copy(e.min), this.max.copy(e.max), this;
  }
  makeEmpty() {
    return this.min.x = this.min.y = this.min.z = 1 / 0, this.max.x = this.max.y = this.max.z = -1 / 0, this;
  }
  isEmpty() {
    return this.max.x < this.min.x || this.max.y < this.min.y || this.max.z < this.min.z;
  }
  getCenter(e) {
    return this.isEmpty() ? e.set(0, 0, 0) : e.addVectors(this.min, this.max).multiplyScalar(0.5);
  }
  getSize(e) {
    return this.isEmpty() ? e.set(0, 0, 0) : e.subVectors(this.max, this.min);
  }
  expandByPoint(e) {
    return this.min.min(e), this.max.max(e), this;
  }
  expandByVector(e) {
    return this.min.sub(e), this.max.add(e), this;
  }
  expandByScalar(e) {
    return this.min.addScalar(-e), this.max.addScalar(e), this;
  }
  expandByObject(e, t = !1) {
    e.updateWorldMatrix(!1, !1);
    const i = e.geometry;
    if (i !== void 0) {
      const s = i.getAttribute("position");
      if (t === !0 && s !== void 0 && e.isInstancedMesh !== !0) for (let a = 0, o = s.count; a < o; a++) e.isMesh === !0 ? e.getVertexPosition(a, di) : di.fromBufferAttribute(s, a), di.applyMatrix4(e.matrixWorld), this.expandByPoint(di);
      else e.boundingBox !== void 0 ? (e.boundingBox === null && e.computeBoundingBox(), xs.copy(e.boundingBox)) : (i.boundingBox === null && i.computeBoundingBox(), xs.copy(i.boundingBox)), xs.applyMatrix4(e.matrixWorld), this.union(xs);
    }
    const n = e.children;
    for (let s = 0, a = n.length; s < a; s++) this.expandByObject(n[s], t);
    return this;
  }
  containsPoint(e) {
    return e.x >= this.min.x && e.x <= this.max.x && e.y >= this.min.y && e.y <= this.max.y && e.z >= this.min.z && e.z <= this.max.z;
  }
  containsBox(e) {
    return this.min.x <= e.min.x && e.max.x <= this.max.x && this.min.y <= e.min.y && e.max.y <= this.max.y && this.min.z <= e.min.z && e.max.z <= this.max.z;
  }
  getParameter(e, t) {
    return t.set((e.x - this.min.x) / (this.max.x - this.min.x), (e.y - this.min.y) / (this.max.y - this.min.y), (e.z - this.min.z) / (this.max.z - this.min.z));
  }
  intersectsBox(e) {
    return e.max.x >= this.min.x && e.min.x <= this.max.x && e.max.y >= this.min.y && e.min.y <= this.max.y && e.max.z >= this.min.z && e.min.z <= this.max.z;
  }
  intersectsSphere(e) {
    return this.clampPoint(e.center, di), di.distanceToSquared(e.center) <= e.radius * e.radius;
  }
  intersectsPlane(e) {
    let t, i;
    return e.normal.x > 0 ? (t = e.normal.x * this.min.x, i = e.normal.x * this.max.x) : (t = e.normal.x * this.max.x, i = e.normal.x * this.min.x), e.normal.y > 0 ? (t += e.normal.y * this.min.y, i += e.normal.y * this.max.y) : (t += e.normal.y * this.max.y, i += e.normal.y * this.min.y), e.normal.z > 0 ? (t += e.normal.z * this.min.z, i += e.normal.z * this.max.z) : (t += e.normal.z * this.max.z, i += e.normal.z * this.min.z), t <= -e.constant && i >= -e.constant;
  }
  intersectsTriangle(e) {
    if (this.isEmpty()) return !1;
    this.getCenter(Cn), Ms.subVectors(this.max, Cn), Wr.subVectors(e.a, Cn), Xr.subVectors(e.b, Cn), jr.subVectors(e.c, Cn), ar.subVectors(Xr, Wr), or.subVectors(jr, Xr), yr.subVectors(Wr, jr);
    let t = [0, -ar.z, ar.y, 0, -or.z, or.y, 0, -yr.z, yr.y, ar.z, 0, -ar.x, or.z, 0, -or.x, yr.z, 0, -yr.x, -ar.y, ar.x, 0, -or.y, or.x, 0, -yr.y, yr.x, 0];
    return !Ka(t, Wr, Xr, jr, Ms) || (t = [1, 0, 0, 0, 1, 0, 0, 0, 1], !Ka(t, Wr, Xr, jr, Ms)) ? !1 : (ys.crossVectors(ar, or), t = [ys.x, ys.y, ys.z], Ka(t, Wr, Xr, jr, Ms));
  }
  clampPoint(e, t) {
    return t.copy(e).clamp(this.min, this.max);
  }
  distanceToPoint(e) {
    return this.clampPoint(e, di).distanceTo(e);
  }
  getBoundingSphere(e) {
    return this.isEmpty() ? e.makeEmpty() : (this.getCenter(e.center), e.radius = this.getSize(di).length() * 0.5), e;
  }
  intersect(e) {
    return this.min.max(e.min), this.max.min(e.max), this.isEmpty() && this.makeEmpty(), this;
  }
  union(e) {
    return this.min.min(e.min), this.max.max(e.max), this;
  }
  applyMatrix4(e) {
    return this.isEmpty() ? this : (Wi[0].set(this.min.x, this.min.y, this.min.z).applyMatrix4(e), Wi[1].set(this.min.x, this.min.y, this.max.z).applyMatrix4(e), Wi[2].set(this.min.x, this.max.y, this.min.z).applyMatrix4(e), Wi[3].set(this.min.x, this.max.y, this.max.z).applyMatrix4(e), Wi[4].set(this.max.x, this.min.y, this.min.z).applyMatrix4(e), Wi[5].set(this.max.x, this.min.y, this.max.z).applyMatrix4(e), Wi[6].set(this.max.x, this.max.y, this.min.z).applyMatrix4(e), Wi[7].set(this.max.x, this.max.y, this.max.z).applyMatrix4(e), this.setFromPoints(Wi), this);
  }
  translate(e) {
    return this.min.add(e), this.max.add(e), this;
  }
  equals(e) {
    return e.min.equals(this.min) && e.max.equals(this.max);
  }
  toJSON() {
    return { min: this.min.toArray(), max: this.max.toArray() };
  }
  fromJSON(e) {
    return this.min.fromArray(e.min), this.max.fromArray(e.max), this;
  }
}
const Wi = [new P(), new P(), new P(), new P(), new P(), new P(), new P(), new P()], di = new P(), xs = new er(), Wr = new P(), Xr = new P(), jr = new P(), ar = new P(), or = new P(), yr = new P(), Cn = new P(), Ms = new P(), ys = new P(), Sr = new P();
function Ka(r, e, t, i, n) {
  for (let s = 0, a = r.length - 3; s <= a; s += 3) {
    Sr.fromArray(r, s);
    const o = n.x * Math.abs(Sr.x) + n.y * Math.abs(Sr.y) + n.z * Math.abs(Sr.z), l = e.dot(Sr), c = t.dot(Sr), h = i.dot(Sr);
    if (Math.max(-Math.max(l, c, h), Math.min(l, c, h)) > o) return !1;
  }
  return !0;
}
const wt = new P(), Ss = new We();
let Af = 0;
class nt extends mr {
  constructor(e, t, i = !1) {
    if (super(), Array.isArray(e)) throw new TypeError("THREE.BufferAttribute: array should be a Typed Array.");
    this.isBufferAttribute = !0, Object.defineProperty(this, "id", { value: Af++ }), this.name = "", this.array = e, this.itemSize = t, this.count = e !== void 0 ? e.length / t : 0, this.normalized = i, this.usage = Hu, this.updateRanges = [], this.gpuType = oi, this.version = 0;
  }
  onUploadCallback() {
  }
  set needsUpdate(e) {
    e === !0 && this.version++;
  }
  setUsage(e) {
    return this.usage = e, this;
  }
  addUpdateRange(e, t) {
    this.updateRanges.push({ start: e, count: t });
  }
  clearUpdateRanges() {
    this.updateRanges.length = 0;
  }
  copy(e) {
    return this.name = e.name, this.array = new e.array.constructor(e.array), this.itemSize = e.itemSize, this.count = e.count, this.normalized = e.normalized, this.usage = e.usage, this.gpuType = e.gpuType, this;
  }
  copyAt(e, t, i) {
    e *= this.itemSize, i *= t.itemSize;
    for (let n = 0, s = this.itemSize; n < s; n++) this.array[e + n] = t.array[i + n];
    return this;
  }
  copyArray(e) {
    return this.array.set(e), this;
  }
  applyMatrix3(e) {
    if (this.itemSize === 2) for (let t = 0, i = this.count; t < i; t++) Ss.fromBufferAttribute(this, t), Ss.applyMatrix3(e), this.setXY(t, Ss.x, Ss.y);
    else if (this.itemSize === 3) for (let t = 0, i = this.count; t < i; t++) wt.fromBufferAttribute(this, t), wt.applyMatrix3(e), this.setXYZ(t, wt.x, wt.y, wt.z);
    return this;
  }
  applyMatrix4(e) {
    for (let t = 0, i = this.count; t < i; t++) wt.fromBufferAttribute(this, t), wt.applyMatrix4(e), this.setXYZ(t, wt.x, wt.y, wt.z);
    return this;
  }
  applyNormalMatrix(e) {
    for (let t = 0, i = this.count; t < i; t++) wt.fromBufferAttribute(this, t), wt.applyNormalMatrix(e), this.setXYZ(t, wt.x, wt.y, wt.z);
    return this;
  }
  transformDirection(e) {
    for (let t = 0, i = this.count; t < i; t++) wt.fromBufferAttribute(this, t), wt.transformDirection(e), this.setXYZ(t, wt.x, wt.y, wt.z);
    return this;
  }
  set(e, t = 0) {
    return this.array.set(e, t), this;
  }
  getComponent(e, t) {
    let i = this.array[e * this.itemSize + t];
    return this.normalized && (i = pi(i, this.array)), i;
  }
  setComponent(e, t, i) {
    return this.normalized && (i = lt(i, this.array)), this.array[e * this.itemSize + t] = i, this;
  }
  getX(e) {
    let t = this.array[e * this.itemSize];
    return this.normalized && (t = pi(t, this.array)), t;
  }
  setX(e, t) {
    return this.normalized && (t = lt(t, this.array)), this.array[e * this.itemSize] = t, this;
  }
  getY(e) {
    let t = this.array[e * this.itemSize + 1];
    return this.normalized && (t = pi(t, this.array)), t;
  }
  setY(e, t) {
    return this.normalized && (t = lt(t, this.array)), this.array[e * this.itemSize + 1] = t, this;
  }
  getZ(e) {
    let t = this.array[e * this.itemSize + 2];
    return this.normalized && (t = pi(t, this.array)), t;
  }
  setZ(e, t) {
    return this.normalized && (t = lt(t, this.array)), this.array[e * this.itemSize + 2] = t, this;
  }
  getW(e) {
    let t = this.array[e * this.itemSize + 3];
    return this.normalized && (t = pi(t, this.array)), t;
  }
  setW(e, t) {
    return this.normalized && (t = lt(t, this.array)), this.array[e * this.itemSize + 3] = t, this;
  }
  setXY(e, t, i) {
    return e *= this.itemSize, this.normalized && (t = lt(t, this.array), i = lt(i, this.array)), this.array[e + 0] = t, this.array[e + 1] = i, this;
  }
  setXYZ(e, t, i, n) {
    return e *= this.itemSize, this.normalized && (t = lt(t, this.array), i = lt(i, this.array), n = lt(n, this.array)), this.array[e + 0] = t, this.array[e + 1] = i, this.array[e + 2] = n, this;
  }
  setXYZW(e, t, i, n, s) {
    return e *= this.itemSize, this.normalized && (t = lt(t, this.array), i = lt(i, this.array), n = lt(n, this.array), s = lt(s, this.array)), this.array[e + 0] = t, this.array[e + 1] = i, this.array[e + 2] = n, this.array[e + 3] = s, this;
  }
  onUpload(e) {
    return this.onUploadCallback = e, this;
  }
  clone() {
    return new this.constructor(this.array, this.itemSize).copy(this);
  }
  toJSON() {
    const e = { itemSize: this.itemSize, type: this.array.constructor.name, array: Array.from(this.array), normalized: this.normalized };
    return e.name = this.name, e.usage = this.usage, e.gpuType = this.gpuType, e;
  }
  dispose() {
    this.dispatchEvent({ type: "dispose" });
  }
}
class $u extends nt {
  constructor(e, t, i) {
    super(new Uint16Array(e), t, i);
  }
}
class Qu extends nt {
  constructor(e, t, i) {
    super(new Uint32Array(e), t, i);
  }
}
class Jt extends nt {
  constructor(e, t, i) {
    super(new Float32Array(e), t, i);
  }
}
const Rf = new er(), Ln = new P(), Za = new P();
class Di {
  constructor(e = new P(), t = -1) {
    this.isSphere = !0, this.center = e, this.radius = t;
  }
  set(e, t) {
    return this.center.copy(e), this.radius = t, this;
  }
  setFromPoints(e, t) {
    const i = this.center;
    t !== void 0 ? i.copy(t) : Rf.setFromPoints(e).getCenter(i);
    let n = 0;
    for (let s = 0, a = e.length; s < a; s++) n = Math.max(n, i.distanceToSquared(e[s]));
    return this.radius = Math.sqrt(n), this;
  }
  copy(e) {
    return this.center.copy(e.center), this.radius = e.radius, this;
  }
  isEmpty() {
    return this.radius < 0;
  }
  makeEmpty() {
    return this.center.set(0, 0, 0), this.radius = -1, this;
  }
  containsPoint(e) {
    return e.distanceToSquared(this.center) <= this.radius * this.radius;
  }
  distanceToPoint(e) {
    return e.distanceTo(this.center) - this.radius;
  }
  intersectsSphere(e) {
    const t = this.radius + e.radius;
    return e.center.distanceToSquared(this.center) <= t * t;
  }
  intersectsBox(e) {
    return e.intersectsSphere(this);
  }
  intersectsPlane(e) {
    return Math.abs(e.distanceToPoint(this.center)) <= this.radius;
  }
  clampPoint(e, t) {
    const i = this.center.distanceToSquared(e);
    return t.copy(e), i > this.radius * this.radius && (t.sub(this.center).normalize(), t.multiplyScalar(this.radius).add(this.center)), t;
  }
  getBoundingBox(e) {
    return this.isEmpty() ? (e.makeEmpty(), e) : (e.set(this.center, this.center), e.expandByScalar(this.radius), e);
  }
  applyMatrix4(e) {
    return this.center.applyMatrix4(e), this.radius = this.radius * e.getMaxScaleOnAxis(), this;
  }
  translate(e) {
    return this.center.add(e), this;
  }
  expandByPoint(e) {
    if (this.isEmpty()) return this.center.copy(e), this.radius = 0, this;
    Ln.subVectors(e, this.center);
    const t = Ln.lengthSq();
    if (t > this.radius * this.radius) {
      const i = Math.sqrt(t), n = (i - this.radius) * 0.5;
      this.center.addScaledVector(Ln, n / i), this.radius += n;
    }
    return this;
  }
  union(e) {
    return e.isEmpty() ? this : this.isEmpty() ? (this.copy(e), this) : (this.center.equals(e.center) === !0 ? this.radius = Math.max(this.radius, e.radius) : (Za.subVectors(e.center, this.center).setLength(e.radius), this.expandByPoint(Ln.copy(e.center).add(Za)), this.expandByPoint(Ln.copy(e.center).sub(Za))), this);
  }
  equals(e) {
    return e.center.equals(this.center) && e.radius === this.radius;
  }
  clone() {
    return new this.constructor().copy(this);
  }
  toJSON() {
    return { radius: this.radius, center: this.center.toArray() };
  }
  fromJSON(e) {
    return this.radius = e.radius, this.center.fromArray(e.center), this;
  }
}
let Pf = 0;
const ri = new Fe(), $a = new at(), qr = new P(), Zt = new er(), In = new er(), It = new P();
class yt extends mr {
  constructor() {
    super(), this.isBufferGeometry = !0, Object.defineProperty(this, "id", { value: Pf++ }), this.uuid = gi(), this.name = "", this.type = "BufferGeometry", this.index = null, this.indirect = null, this.indirectOffset = 0, this.attributes = {}, this.morphAttributes = {}, this.morphTargetsRelative = !1, this.groups = [], this.boundingBox = null, this.boundingSphere = null, this.drawRange = { start: 0, count: 1 / 0 }, this.userData = {}, this._transformed = !1;
  }
  getIndex() {
    return this.index;
  }
  setIndex(e) {
    return Array.isArray(e) ? this.index = new (Xp(e) ? Qu : $u)(e, 1) : this.index = e, this;
  }
  setIndirect(e, t = 0) {
    return this.indirect = e, this.indirectOffset = t, this;
  }
  getIndirect() {
    return this.indirect;
  }
  getAttribute(e) {
    return this.attributes[e];
  }
  setAttribute(e, t) {
    return this.attributes[e] = t, this;
  }
  deleteAttribute(e) {
    return delete this.attributes[e], this;
  }
  hasAttribute(e) {
    return this.attributes[e] !== void 0;
  }
  addGroup(e, t, i = 0) {
    this.groups.push({ start: e, count: t, materialIndex: i });
  }
  clearGroups() {
    this.groups = [];
  }
  setDrawRange(e, t) {
    this.drawRange.start = e, this.drawRange.count = t;
  }
  applyMatrix4(e) {
    const t = this.attributes.position;
    t !== void 0 && (t.applyMatrix4(e), t.needsUpdate = !0);
    const i = this.attributes.normal;
    if (i !== void 0) {
      const s = new De().getNormalMatrix(e);
      i.applyNormalMatrix(s), i.needsUpdate = !0;
    }
    const n = this.attributes.tangent;
    return n !== void 0 && (n.transformDirection(e), n.needsUpdate = !0), this.boundingBox !== null && this.computeBoundingBox(), this.boundingSphere !== null && this.computeBoundingSphere(), this._transformed = !0, this;
  }
  applyQuaternion(e) {
    return ri.makeRotationFromQuaternion(e), this.applyMatrix4(ri), this;
  }
  rotateX(e) {
    return ri.makeRotationX(e), this.applyMatrix4(ri), this;
  }
  rotateY(e) {
    return ri.makeRotationY(e), this.applyMatrix4(ri), this;
  }
  rotateZ(e) {
    return ri.makeRotationZ(e), this.applyMatrix4(ri), this;
  }
  translate(e, t, i) {
    return ri.makeTranslation(e, t, i), this.applyMatrix4(ri), this;
  }
  scale(e, t, i) {
    return ri.makeScale(e, t, i), this.applyMatrix4(ri), this;
  }
  lookAt(e) {
    return $a.lookAt(e), $a.updateMatrix(), this.applyMatrix4($a.matrix), this;
  }
  center() {
    return this.computeBoundingBox(), this.boundingBox.getCenter(qr).negate(), this.translate(qr.x, qr.y, qr.z), this;
  }
  setFromPoints(e) {
    const t = this.getAttribute("position");
    if (t === void 0) {
      const i = [];
      for (let n = 0, s = e.length; n < s; n++) {
        const a = e[n];
        i.push(a.x, a.y, a.z || 0);
      }
      this.setAttribute("position", new Jt(i, 3));
    } else {
      const i = Math.min(e.length, t.count);
      for (let n = 0; n < i; n++) {
        const s = e[n];
        t.setXYZ(n, s.x, s.y, s.z || 0);
      }
      e.length > t.count && Re("BufferGeometry: Buffer size too small for points data. Use .dispose() and create a new geometry."), t.needsUpdate = !0;
    }
    return this;
  }
  computeBoundingBox() {
    this.boundingBox === null && (this.boundingBox = new er());
    const e = this.attributes.position, t = this.morphAttributes.position;
    if (e && e.isGLBufferAttribute) {
      Be("BufferGeometry.computeBoundingBox(): GLBufferAttribute requires a manual bounding box.", this), this.boundingBox.set(new P(-1 / 0, -1 / 0, -1 / 0), new P(1 / 0, 1 / 0, 1 / 0));
      return;
    }
    if (e !== void 0) {
      if (this.boundingBox.setFromBufferAttribute(e), t) for (let i = 0, n = t.length; i < n; i++) {
        const s = t[i];
        Zt.setFromBufferAttribute(s), this.morphTargetsRelative ? (It.addVectors(this.boundingBox.min, Zt.min), this.boundingBox.expandByPoint(It), It.addVectors(this.boundingBox.max, Zt.max), this.boundingBox.expandByPoint(It)) : (this.boundingBox.expandByPoint(Zt.min), this.boundingBox.expandByPoint(Zt.max));
      }
    } else this.boundingBox.makeEmpty();
    (isNaN(this.boundingBox.min.x) || isNaN(this.boundingBox.min.y) || isNaN(this.boundingBox.min.z)) && Be('BufferGeometry.computeBoundingBox(): Computed min/max have NaN values. The "position" attribute is likely to have NaN values.', this);
  }
  computeBoundingSphere() {
    this.boundingSphere === null && (this.boundingSphere = new Di());
    const e = this.attributes.position, t = this.morphAttributes.position;
    if (e && e.isGLBufferAttribute) {
      Be("BufferGeometry.computeBoundingSphere(): GLBufferAttribute requires a manual bounding sphere.", this), this.boundingSphere.set(new P(), 1 / 0);
      return;
    }
    if (e) {
      const i = this.boundingSphere.center;
      if (Zt.setFromBufferAttribute(e), t) for (let s = 0, a = t.length; s < a; s++) {
        const o = t[s];
        In.setFromBufferAttribute(o), this.morphTargetsRelative ? (It.addVectors(Zt.min, In.min), Zt.expandByPoint(It), It.addVectors(Zt.max, In.max), Zt.expandByPoint(It)) : (Zt.expandByPoint(In.min), Zt.expandByPoint(In.max));
      }
      Zt.getCenter(i);
      let n = 0;
      for (let s = 0, a = e.count; s < a; s++) It.fromBufferAttribute(e, s), n = Math.max(n, i.distanceToSquared(It));
      if (t) for (let s = 0, a = t.length; s < a; s++) {
        const o = t[s], l = this.morphTargetsRelative;
        for (let c = 0, h = o.count; c < h; c++) It.fromBufferAttribute(o, c), l && (qr.fromBufferAttribute(e, c), It.add(qr)), n = Math.max(n, i.distanceToSquared(It));
      }
      this.boundingSphere.radius = Math.sqrt(n), isNaN(this.boundingSphere.radius) && Be('BufferGeometry.computeBoundingSphere(): Computed radius is NaN. The "position" attribute is likely to have NaN values.', this);
    }
  }
  computeTangents() {
    const e = this.index, t = this.attributes;
    if (e === null || t.position === void 0 || t.normal === void 0 || t.uv === void 0) {
      Be("BufferGeometry: .computeTangents() failed. Missing required attributes (index, position, normal or uv)");
      return;
    }
    const i = t.position, n = t.normal, s = t.uv;
    let a = this.getAttribute("tangent");
    (a === void 0 || a.count !== i.count) && (a = new nt(new Float32Array(4 * i.count), 4), this.setAttribute("tangent", a));
    const o = [], l = [];
    for (let x = 0; x < i.count; x++) o[x] = new P(), l[x] = new P();
    const c = new P(), h = new P(), d = new P(), u = new We(), p = new We(), f = new We(), v = new P(), m = new P();
    function g(x, b, N) {
      c.fromBufferAttribute(i, x), h.fromBufferAttribute(i, b), d.fromBufferAttribute(i, N), u.fromBufferAttribute(s, x), p.fromBufferAttribute(s, b), f.fromBufferAttribute(s, N), h.sub(c), d.sub(c), p.sub(u), f.sub(u);
      const O = 1 / (p.x * f.y - f.x * p.y);
      isFinite(O) && (v.copy(h).multiplyScalar(f.y).addScaledVector(d, -p.y).multiplyScalar(O), m.copy(d).multiplyScalar(p.x).addScaledVector(h, -f.x).multiplyScalar(O), o[x].add(v), o[b].add(v), o[N].add(v), l[x].add(m), l[b].add(m), l[N].add(m));
    }
    let E = this.groups;
    E.length === 0 && (E = [{ start: 0, count: e.count }]);
    for (let x = 0, b = E.length; x < b; ++x) {
      const N = E[x], O = N.start, k = N.count;
      for (let q = O, B = O + k; q < B; q += 3) g(e.getX(q + 0), e.getX(q + 1), e.getX(q + 2));
    }
    const R = new P(), y = new P(), S = new P(), T = new P();
    function A(x) {
      S.fromBufferAttribute(n, x), T.copy(S);
      const b = o[x];
      R.copy(b), R.sub(S.multiplyScalar(S.dot(b))).normalize(), y.crossVectors(T, b);
      const N = y.dot(l[x]) < 0 ? -1 : 1;
      a.setXYZW(x, R.x, R.y, R.z, N);
    }
    for (let x = 0, b = E.length; x < b; ++x) {
      const N = E[x], O = N.start, k = N.count;
      for (let q = O, B = O + k; q < B; q += 3) A(e.getX(q + 0)), A(e.getX(q + 1)), A(e.getX(q + 2));
    }
    this._transformed = !0;
  }
  computeVertexNormals() {
    const e = this.index, t = this.getAttribute("position");
    if (t !== void 0) {
      let i = this.getAttribute("normal");
      if (i === void 0 || i.count !== t.count) i = new nt(new Float32Array(t.count * 3), 3), this.setAttribute("normal", i);
      else for (let u = 0, p = i.count; u < p; u++) i.setXYZ(u, 0, 0, 0);
      const n = new P(), s = new P(), a = new P(), o = new P(), l = new P(), c = new P(), h = new P(), d = new P();
      if (e) for (let u = 0, p = e.count; u < p; u += 3) {
        const f = e.getX(u + 0), v = e.getX(u + 1), m = e.getX(u + 2);
        n.fromBufferAttribute(t, f), s.fromBufferAttribute(t, v), a.fromBufferAttribute(t, m), h.subVectors(a, s), d.subVectors(n, s), h.cross(d), o.fromBufferAttribute(i, f), l.fromBufferAttribute(i, v), c.fromBufferAttribute(i, m), o.add(h), l.add(h), c.add(h), i.setXYZ(f, o.x, o.y, o.z), i.setXYZ(v, l.x, l.y, l.z), i.setXYZ(m, c.x, c.y, c.z);
      }
      else for (let u = 0, p = t.count; u < p; u += 3) n.fromBufferAttribute(t, u + 0), s.fromBufferAttribute(t, u + 1), a.fromBufferAttribute(t, u + 2), h.subVectors(a, s), d.subVectors(n, s), h.cross(d), i.setXYZ(u + 0, h.x, h.y, h.z), i.setXYZ(u + 1, h.x, h.y, h.z), i.setXYZ(u + 2, h.x, h.y, h.z);
      this.normalizeNormals(), i.needsUpdate = !0;
    }
  }
  normalizeNormals() {
    const e = this.attributes.normal;
    for (let t = 0, i = e.count; t < i; t++) It.fromBufferAttribute(e, t), It.normalize(), e.setXYZ(t, It.x, It.y, It.z);
  }
  toNonIndexed() {
    function e(o, l) {
      const c = o.array, h = o.itemSize, d = o.normalized, u = new c.constructor(l.length * h);
      let p = 0, f = 0;
      for (let v = 0, m = l.length; v < m; v++) {
        o.isInterleavedBufferAttribute ? p = l[v] * o.data.stride + o.offset : p = l[v] * h;
        for (let g = 0; g < h; g++) u[f++] = c[p++];
      }
      return new nt(u, h, d);
    }
    if (this.index === null) return Re("BufferGeometry.toNonIndexed(): BufferGeometry is already non-indexed."), this;
    const t = new yt(), i = this.index.array, n = this.attributes;
    for (const o in n) {
      const l = n[o], c = e(l, i);
      t.setAttribute(o, c);
    }
    const s = this.morphAttributes;
    for (const o in s) {
      const l = [], c = s[o];
      for (let h = 0, d = c.length; h < d; h++) {
        const u = c[h], p = e(u, i);
        l.push(p);
      }
      t.morphAttributes[o] = l;
    }
    t.morphTargetsRelative = this.morphTargetsRelative;
    const a = this.groups;
    for (let o = 0, l = a.length; o < l; o++) {
      const c = a[o];
      t.addGroup(c.start, c.count, c.materialIndex);
    }
    return t;
  }
  toJSON() {
    const e = { metadata: { version: 4.7, type: "BufferGeometry", generator: "BufferGeometry.toJSON" } };
    if (e.uuid = this.uuid, e.type = this.parameters !== void 0 && this._transformed === !0 ? "BufferGeometry" : this.type, e.name = this.name, Object.keys(this.userData).length > 0 && (e.userData = this.userData), this.parameters !== void 0 && this._transformed !== !0) {
      const l = this.parameters;
      for (const c in l) l[c] !== void 0 && (e[c] = l[c]);
      return e;
    }
    e.data = { attributes: {} };
    const t = this.index;
    t !== null && (e.data.index = { type: t.array.constructor.name, array: Array.prototype.slice.call(t.array) });
    const i = this.attributes;
    for (const l in i) {
      const c = i[l];
      e.data.attributes[l] = c.toJSON(e.data);
    }
    const n = {};
    let s = !1;
    for (const l in this.morphAttributes) {
      const c = this.morphAttributes[l], h = [];
      for (let d = 0, u = c.length; d < u; d++) {
        const p = c[d];
        h.push(p.toJSON(e.data));
      }
      h.length > 0 && (n[l] = h, s = !0);
    }
    s && (e.data.morphAttributes = n, e.data.morphTargetsRelative = this.morphTargetsRelative);
    const a = this.groups;
    a.length > 0 && (e.data.groups = JSON.parse(JSON.stringify(a)));
    const o = this.boundingSphere;
    return o !== null && (e.data.boundingSphere = o.toJSON()), e;
  }
  clone() {
    return new this.constructor().copy(this);
  }
  copy(e) {
    this.index = null, this.attributes = {}, this.morphAttributes = {}, this.groups = [], this.boundingBox = null, this.boundingSphere = null;
    const t = {};
    this.name = e.name;
    const i = e.index;
    i !== null && this.setIndex(i.clone());
    const n = e.attributes;
    for (const c in n) {
      const h = n[c];
      this.setAttribute(c, h.clone(t));
    }
    const s = e.morphAttributes;
    for (const c in s) {
      const h = [], d = s[c];
      for (let u = 0, p = d.length; u < p; u++) h.push(d[u].clone(t));
      this.morphAttributes[c] = h;
    }
    this.morphTargetsRelative = e.morphTargetsRelative;
    const a = e.groups;
    for (let c = 0, h = a.length; c < h; c++) {
      const d = a[c];
      this.addGroup(d.start, d.count, d.materialIndex);
    }
    const o = e.boundingBox;
    o !== null && (this.boundingBox = o.clone());
    const l = e.boundingSphere;
    return l !== null && (this.boundingSphere = l.clone()), this.drawRange.start = e.drawRange.start, this.drawRange.count = e.drawRange.count, this.userData = e.userData, this._transformed = e._transformed, this;
  }
  dispose() {
    this.dispatchEvent({ type: "dispose" });
  }
}
class ql {
  constructor(e, t) {
    this.isInterleavedBuffer = !0, this.array = e, this.stride = t, this.count = e !== void 0 ? e.length / t : 0, this.usage = Hu, this.updateRanges = [], this.version = 0, this.uuid = gi();
  }
  onUploadCallback() {
  }
  set needsUpdate(e) {
    e === !0 && this.version++;
  }
  setUsage(e) {
    return this.usage = e, this;
  }
  addUpdateRange(e, t) {
    this.updateRanges.push({ start: e, count: t });
  }
  clearUpdateRanges() {
    this.updateRanges.length = 0;
  }
  copy(e) {
    return this.array = new e.array.constructor(e.array), this.count = e.count, this.stride = e.stride, this.usage = e.usage, this;
  }
  copyAt(e, t, i) {
    e *= this.stride, i *= t.stride;
    for (let n = 0, s = this.stride; n < s; n++) this.array[e + n] = t.array[i + n];
    return this;
  }
  set(e, t = 0) {
    return this.array.set(e, t), this;
  }
  clone(e) {
    e.arrayBuffers === void 0 && (e.arrayBuffers = {}), this.array.buffer._uuid === void 0 && (this.array.buffer._uuid = gi()), e.arrayBuffers[this.array.buffer._uuid] === void 0 && (e.arrayBuffers[this.array.buffer._uuid] = this.array.slice(0).buffer);
    const t = new this.array.constructor(e.arrayBuffers[this.array.buffer._uuid]), i = new this.constructor(t, this.stride);
    return i.setUsage(this.usage), i;
  }
  onUpload(e) {
    return this.onUploadCallback = e, this;
  }
  toJSON(e) {
    e.arrayBuffers === void 0 && (e.arrayBuffers = {}), this.array.buffer._uuid === void 0 && (this.array.buffer._uuid = gi()), e.arrayBuffers[this.array.buffer._uuid] === void 0 && (e.arrayBuffers[this.array.buffer._uuid] = Array.from(new Uint32Array(this.array.buffer)));
    const t = { uuid: this.uuid, buffer: this.array.buffer._uuid, type: this.array.constructor.name, stride: this.stride };
    return t.usage = this.usage, t;
  }
}
const Ht = new P();
class us {
  constructor(e, t, i, n = !1) {
    this.isInterleavedBufferAttribute = !0, this.name = "", this.data = e, this.itemSize = t, this.offset = i, this.normalized = n;
  }
  get count() {
    return this.data.count;
  }
  get array() {
    return this.data.array;
  }
  set needsUpdate(e) {
    this.data.needsUpdate = e;
  }
  applyMatrix4(e) {
    for (let t = 0, i = this.data.count; t < i; t++) Ht.fromBufferAttribute(this, t), Ht.applyMatrix4(e), this.setXYZ(t, Ht.x, Ht.y, Ht.z);
    return this;
  }
  applyNormalMatrix(e) {
    for (let t = 0, i = this.count; t < i; t++) Ht.fromBufferAttribute(this, t), Ht.applyNormalMatrix(e), this.setXYZ(t, Ht.x, Ht.y, Ht.z);
    return this;
  }
  transformDirection(e) {
    for (let t = 0, i = this.count; t < i; t++) Ht.fromBufferAttribute(this, t), Ht.transformDirection(e), this.setXYZ(t, Ht.x, Ht.y, Ht.z);
    return this;
  }
  getComponent(e, t) {
    let i = this.array[e * this.data.stride + this.offset + t];
    return this.normalized && (i = pi(i, this.array)), i;
  }
  setComponent(e, t, i) {
    return this.normalized && (i = lt(i, this.array)), this.data.array[e * this.data.stride + this.offset + t] = i, this;
  }
  setX(e, t) {
    return this.normalized && (t = lt(t, this.array)), this.data.array[e * this.data.stride + this.offset] = t, this;
  }
  setY(e, t) {
    return this.normalized && (t = lt(t, this.array)), this.data.array[e * this.data.stride + this.offset + 1] = t, this;
  }
  setZ(e, t) {
    return this.normalized && (t = lt(t, this.array)), this.data.array[e * this.data.stride + this.offset + 2] = t, this;
  }
  setW(e, t) {
    return this.normalized && (t = lt(t, this.array)), this.data.array[e * this.data.stride + this.offset + 3] = t, this;
  }
  getX(e) {
    let t = this.data.array[e * this.data.stride + this.offset];
    return this.normalized && (t = pi(t, this.array)), t;
  }
  getY(e) {
    let t = this.data.array[e * this.data.stride + this.offset + 1];
    return this.normalized && (t = pi(t, this.array)), t;
  }
  getZ(e) {
    let t = this.data.array[e * this.data.stride + this.offset + 2];
    return this.normalized && (t = pi(t, this.array)), t;
  }
  getW(e) {
    let t = this.data.array[e * this.data.stride + this.offset + 3];
    return this.normalized && (t = pi(t, this.array)), t;
  }
  setXY(e, t, i) {
    return e = e * this.data.stride + this.offset, this.normalized && (t = lt(t, this.array), i = lt(i, this.array)), this.data.array[e + 0] = t, this.data.array[e + 1] = i, this;
  }
  setXYZ(e, t, i, n) {
    return e = e * this.data.stride + this.offset, this.normalized && (t = lt(t, this.array), i = lt(i, this.array), n = lt(n, this.array)), this.data.array[e + 0] = t, this.data.array[e + 1] = i, this.data.array[e + 2] = n, this;
  }
  setXYZW(e, t, i, n, s) {
    return e = e * this.data.stride + this.offset, this.normalized && (t = lt(t, this.array), i = lt(i, this.array), n = lt(n, this.array), s = lt(s, this.array)), this.data.array[e + 0] = t, this.data.array[e + 1] = i, this.data.array[e + 2] = n, this.data.array[e + 3] = s, this;
  }
  clone(e) {
    if (e === void 0) {
      ha("InterleavedBufferAttribute.clone(): Cloning an interleaved buffer attribute will de-interleave buffer data.");
      const t = [];
      for (let i = 0; i < this.count; i++) {
        const n = i * this.data.stride + this.offset;
        for (let s = 0; s < this.itemSize; s++) t.push(this.data.array[n + s]);
      }
      return new nt(new this.array.constructor(t), this.itemSize, this.normalized);
    } else return e.interleavedBuffers === void 0 && (e.interleavedBuffers = {}), e.interleavedBuffers[this.data.uuid] === void 0 && (e.interleavedBuffers[this.data.uuid] = this.data.clone(e)), new us(e.interleavedBuffers[this.data.uuid], this.itemSize, this.offset, this.normalized);
  }
  toJSON(e) {
    if (e === void 0) {
      ha("InterleavedBufferAttribute.toJSON(): Serializing an interleaved buffer attribute will de-interleave buffer data.");
      const t = [];
      for (let i = 0; i < this.count; i++) {
        const n = i * this.data.stride + this.offset;
        for (let s = 0; s < this.itemSize; s++) t.push(this.data.array[n + s]);
      }
      return { itemSize: this.itemSize, type: this.array.constructor.name, array: t, normalized: this.normalized };
    } else return e.interleavedBuffers === void 0 && (e.interleavedBuffers = {}), e.interleavedBuffers[this.data.uuid] === void 0 && (e.interleavedBuffers[this.data.uuid] = this.data.toJSON(e)), { isInterleavedBufferAttribute: !0, itemSize: this.itemSize, data: this.data.uuid, offset: this.offset, normalized: this.normalized };
  }
}
const Qa = new P(), Cf = new P(), Lf = new De();
class ur {
  constructor(e = new P(1, 0, 0), t = 0) {
    this.isPlane = !0, this.normal = e, this.constant = t;
  }
  set(e, t) {
    return this.normal.copy(e), this.constant = t, this;
  }
  setComponents(e, t, i, n) {
    return this.normal.set(e, t, i), this.constant = n, this;
  }
  setFromNormalAndCoplanarPoint(e, t) {
    return this.normal.copy(e), this.constant = -t.dot(this.normal), this;
  }
  setFromCoplanarPoints(e, t, i) {
    const n = Qa.subVectors(i, t).cross(Cf.subVectors(e, t)).normalize();
    return this.setFromNormalAndCoplanarPoint(n, e), this;
  }
  copy(e) {
    return this.normal.copy(e.normal), this.constant = e.constant, this;
  }
  normalize() {
    const e = 1 / this.normal.length();
    return this.normal.multiplyScalar(e), this.constant *= e, this;
  }
  negate() {
    return this.constant *= -1, this.normal.negate(), this;
  }
  distanceToPoint(e) {
    return this.normal.dot(e) + this.constant;
  }
  distanceToSphere(e) {
    return this.distanceToPoint(e.center) - e.radius;
  }
  projectPoint(e, t) {
    return t.copy(e).addScaledVector(this.normal, -this.distanceToPoint(e));
  }
  intersectLine(e, t, i = !0) {
    const n = e.delta(Qa), s = this.normal.dot(n);
    if (s === 0) return this.distanceToPoint(e.start) === 0 ? t.copy(e.start) : null;
    const a = -(e.start.dot(this.normal) + this.constant) / s;
    return i === !0 && (a < 0 || a > 1) ? null : t.copy(e.start).addScaledVector(n, a);
  }
  intersectsLine(e) {
    const t = this.distanceToPoint(e.start), i = this.distanceToPoint(e.end);
    return t < 0 && i > 0 || i < 0 && t > 0;
  }
  intersectsBox(e) {
    return e.intersectsPlane(this);
  }
  intersectsSphere(e) {
    return e.intersectsPlane(this);
  }
  coplanarPoint(e) {
    return e.copy(this.normal).multiplyScalar(-this.constant);
  }
  applyMatrix4(e, t) {
    const i = t || Lf.getNormalMatrix(e), n = this.coplanarPoint(Qa).applyMatrix4(e), s = this.normal.applyMatrix3(i).normalize();
    return this.constant = -n.dot(s), this;
  }
  translate(e) {
    return this.constant -= e.dot(this.normal), this;
  }
  equals(e) {
    return e.normal.equals(this.normal) && e.constant === this.constant;
  }
  clone() {
    return new this.constructor().copy(this);
  }
  toJSON() {
    return { normal: this.normal.toArray(), constant: this.constant };
  }
  fromJSON(e) {
    return this.normal.fromArray(e.normal), this.constant = e.constant, this;
  }
}
let If = 0;
class vi extends mr {
  constructor() {
    super(), this.isMaterial = !0, Object.defineProperty(this, "id", { value: If++ }), this.uuid = gi(), this.name = "", this.type = "Material", this.blending = Qn, this.side = fr, this.vertexColors = !1, this.opacity = 1, this.transparent = !1, this.alphaHash = !1, this.blendSrc = yu, this.blendDst = Su, this.blendEquation = nn, this.blendSrcAlpha = null, this.blendDstAlpha = null, this.blendEquationAlpha = null, this.blendColor = new be(0, 0, 0), this.blendAlpha = 0, this.depthFunc = is, this.depthTest = !0, this.depthWrite = !0, this.stencilWriteMask = 255, this.stencilFunc = Fp, this.stencilRef = 0, this.stencilFuncMask = 255, this.stencilFail = Ua, this.stencilZFail = Ua, this.stencilZPass = Ua, this.stencilWrite = !1, this.clippingPlanes = null, this.clipIntersection = !1, this.clipShadows = !1, this.shadowSide = null, this.colorWrite = !0, this.precision = null, this.polygonOffset = !1, this.polygonOffsetFactor = 0, this.polygonOffsetUnits = 0, this.dithering = !1, this.alphaToCoverage = !1, this.premultipliedAlpha = !1, this.forceSinglePass = !1, this.allowOverride = !0, this.visible = !0, this.toneMapped = !0, this.userData = {}, this.version = 0, this._alphaTest = 0;
  }
  get alphaTest() {
    return this._alphaTest;
  }
  set alphaTest(e) {
    this._alphaTest > 0 != e > 0 && this.version++, this._alphaTest = e;
  }
  onBeforeRender() {
  }
  onBeforeCompile() {
  }
  customProgramCacheKey() {
    return this.onBeforeCompile.toString();
  }
  setValues(e) {
    if (e !== void 0) for (const t in e) {
      const i = e[t];
      if (i === void 0) {
        Re(`Material: parameter '${t}' has value of undefined.`);
        continue;
      }
      const n = this[t];
      if (n === void 0) {
        Re(`Material: '${t}' is not a property of THREE.${this.type}.`);
        continue;
      }
      n && n.isColor ? n.set(i) : n && n.isVector2 && i && i.isVector2 || n && n.isEuler && i && i.isEuler || n && n.isVector3 && i && i.isVector3 ? n.copy(i) : this[t] = i;
    }
  }
  toJSON(e) {
    const t = e === void 0 || typeof e == "string";
    t && (e = { textures: {}, images: {} });
    const i = { metadata: { version: 4.7, type: "Material", generator: "Material.toJSON" } };
    i.uuid = this.uuid, i.type = this.type, i.blending = this.blending, i.side = this.side, i.shadowSide = this.shadowSide, i.vertexColors = this.vertexColors, i.opacity = this.opacity, i.transparent = this.transparent, i.blendSrc = this.blendSrc, i.blendDst = this.blendDst, i.blendEquation = this.blendEquation, i.blendSrcAlpha = this.blendSrcAlpha, i.blendDstAlpha = this.blendDstAlpha, i.blendEquationAlpha = this.blendEquationAlpha, i.blendColor = this.blendColor.getHex(), i.blendAlpha = this.blendAlpha, i.depthFunc = this.depthFunc, i.depthTest = this.depthTest, i.depthWrite = this.depthWrite, i.colorWrite = this.colorWrite, i.clipIntersection = this.clipIntersection, i.clipShadows = this.clipShadows, i.stencilWriteMask = this.stencilWriteMask, i.stencilFunc = this.stencilFunc, i.stencilRef = this.stencilRef, i.stencilFuncMask = this.stencilFuncMask, i.stencilFail = this.stencilFail, i.stencilZFail = this.stencilZFail, i.stencilZPass = this.stencilZPass, i.stencilWrite = this.stencilWrite, i.polygonOffset = this.polygonOffset, i.polygonOffsetFactor = this.polygonOffsetFactor, i.polygonOffsetUnits = this.polygonOffsetUnits, i.dithering = this.dithering, i.alphaTest = this.alphaTest, i.alphaHash = this.alphaHash, i.alphaToCoverage = this.alphaToCoverage, i.premultipliedAlpha = this.premultipliedAlpha, i.forceSinglePass = this.forceSinglePass, i.allowOverride = this.allowOverride, i.visible = this.visible, i.toneMapped = this.toneMapped, i.name = this.name, this.color && this.color.isColor && (i.color = this.color.getHex()), this.roughness !== void 0 && (i.roughness = this.roughness), this.metalness !== void 0 && (i.metalness = this.metalness), this.sheen !== void 0 && (i.sheen = this.sheen), this.sheenColor && this.sheenColor.isColor && (i.sheenColor = this.sheenColor.getHex()), this.sheenRoughness !== void 0 && (i.sheenRoughness = this.sheenRoughness), this.emissive && this.emissive.isColor && (i.emissive = this.emissive.getHex()), this.emissiveIntensity !== void 0 && (i.emissiveIntensity = this.emissiveIntensity), this.specular && this.specular.isColor && (i.specular = this.specular.getHex()), this.specularIntensity !== void 0 && (i.specularIntensity = this.specularIntensity), this.specularColor && this.specularColor.isColor && (i.specularColor = this.specularColor.getHex()), this.shininess !== void 0 && (i.shininess = this.shininess), this.clearcoat !== void 0 && (i.clearcoat = this.clearcoat), this.clearcoatRoughness !== void 0 && (i.clearcoatRoughness = this.clearcoatRoughness), this.clearcoatMap && this.clearcoatMap.isTexture && (i.clearcoatMap = this.clearcoatMap.toJSON(e).uuid), this.clearcoatRoughnessMap && this.clearcoatRoughnessMap.isTexture && (i.clearcoatRoughnessMap = this.clearcoatRoughnessMap.toJSON(e).uuid), this.clearcoatNormalMap && this.clearcoatNormalMap.isTexture && (i.clearcoatNormalMap = this.clearcoatNormalMap.toJSON(e).uuid, i.clearcoatNormalScale = this.clearcoatNormalScale.toArray()), this.sheenColorMap && this.sheenColorMap.isTexture && (i.sheenColorMap = this.sheenColorMap.toJSON(e).uuid), this.sheenRoughnessMap && this.sheenRoughnessMap.isTexture && (i.sheenRoughnessMap = this.sheenRoughnessMap.toJSON(e).uuid), this.dispersion !== void 0 && (i.dispersion = this.dispersion), this.retroreflectivity !== void 0 && (i.retroreflectivity = this.retroreflectivity), this.iridescence !== void 0 && (i.iridescence = this.iridescence), this.iridescenceIOR !== void 0 && (i.iridescenceIOR = this.iridescenceIOR), this.iridescenceThicknessRange !== void 0 && (i.iridescenceThicknessRange = this.iridescenceThicknessRange), this.iridescenceMap && this.iridescenceMap.isTexture && (i.iridescenceMap = this.iridescenceMap.toJSON(e).uuid), this.iridescenceThicknessMap && this.iridescenceThicknessMap.isTexture && (i.iridescenceThicknessMap = this.iridescenceThicknessMap.toJSON(e).uuid), this.anisotropy !== void 0 && (i.anisotropy = this.anisotropy), this.anisotropyRotation !== void 0 && (i.anisotropyRotation = this.anisotropyRotation), this.anisotropyMap && this.anisotropyMap.isTexture && (i.anisotropyMap = this.anisotropyMap.toJSON(e).uuid), this.map && this.map.isTexture && (i.map = this.map.toJSON(e).uuid), this.matcap && this.matcap.isTexture && (i.matcap = this.matcap.toJSON(e).uuid), this.alphaMap && this.alphaMap.isTexture && (i.alphaMap = this.alphaMap.toJSON(e).uuid), this.lightMap && this.lightMap.isTexture && (i.lightMap = this.lightMap.toJSON(e).uuid, i.lightMapIntensity = this.lightMapIntensity), this.aoMap && this.aoMap.isTexture && (i.aoMap = this.aoMap.toJSON(e).uuid, i.aoMapIntensity = this.aoMapIntensity), this.bumpMap && this.bumpMap.isTexture && (i.bumpMap = this.bumpMap.toJSON(e).uuid, i.bumpScale = this.bumpScale), this.normalMap && this.normalMap.isTexture && (i.normalMap = this.normalMap.toJSON(e).uuid, i.normalMapType = this.normalMapType, i.normalScale = this.normalScale.toArray()), this.displacementMap && this.displacementMap.isTexture && (i.displacementMap = this.displacementMap.toJSON(e).uuid, i.displacementScale = this.displacementScale, i.displacementBias = this.displacementBias), this.roughnessMap && this.roughnessMap.isTexture && (i.roughnessMap = this.roughnessMap.toJSON(e).uuid), this.metalnessMap && this.metalnessMap.isTexture && (i.metalnessMap = this.metalnessMap.toJSON(e).uuid), this.emissiveMap && this.emissiveMap.isTexture && (i.emissiveMap = this.emissiveMap.toJSON(e).uuid), this.specularMap && this.specularMap.isTexture && (i.specularMap = this.specularMap.toJSON(e).uuid), this.specularIntensityMap && this.specularIntensityMap.isTexture && (i.specularIntensityMap = this.specularIntensityMap.toJSON(e).uuid), this.specularColorMap && this.specularColorMap.isTexture && (i.specularColorMap = this.specularColorMap.toJSON(e).uuid), this.envMap && this.envMap.isTexture && (i.envMap = this.envMap.toJSON(e).uuid, this.combine !== void 0 && (i.combine = this.combine)), this.envMapRotation !== void 0 && (i.envMapRotation = this.envMapRotation.toArray()), this.envMapIntensity !== void 0 && (i.envMapIntensity = this.envMapIntensity), this.reflectivity !== void 0 && (i.reflectivity = this.reflectivity), this.refractionRatio !== void 0 && (i.refractionRatio = this.refractionRatio), this.gradientMap && this.gradientMap.isTexture && (i.gradientMap = this.gradientMap.toJSON(e).uuid), this.transmission !== void 0 && (i.transmission = this.transmission), this.transmissionMap && this.transmissionMap.isTexture && (i.transmissionMap = this.transmissionMap.toJSON(e).uuid), this.thickness !== void 0 && (i.thickness = this.thickness), this.thicknessMap && this.thicknessMap.isTexture && (i.thicknessMap = this.thicknessMap.toJSON(e).uuid), this.attenuationDistance !== void 0 && (i.attenuationDistance = this.attenuationDistance), this.attenuationColor !== void 0 && (i.attenuationColor = this.attenuationColor.getHex()), this.size !== void 0 && (i.size = this.size), this.sizeAttenuation !== void 0 && (i.sizeAttenuation = this.sizeAttenuation), Array.isArray(this.clippingPlanes) && this.clippingPlanes.length > 0 && (i.clippingPlanes = this.clippingPlanes.map((s) => s.toJSON())), this.rotation !== void 0 && (i.rotation = this.rotation), this.depthPacking !== void 0 && (i.depthPacking = this.depthPacking), this.linewidth !== void 0 && (i.linewidth = this.linewidth), this.linecap !== void 0 && (i.linecap = this.linecap), this.linejoin !== void 0 && (i.linejoin = this.linejoin), this.dashSize !== void 0 && (i.dashSize = this.dashSize), this.gapSize !== void 0 && (i.gapSize = this.gapSize), this.scale !== void 0 && (i.scale = this.scale), this.wireframe !== void 0 && (i.wireframe = this.wireframe), this.wireframeLinewidth !== void 0 && (i.wireframeLinewidth = this.wireframeLinewidth), this.wireframeLinecap !== void 0 && (i.wireframeLinecap = this.wireframeLinecap), this.wireframeLinejoin !== void 0 && (i.wireframeLinejoin = this.wireframeLinejoin), this.flatShading !== void 0 && (i.flatShading = this.flatShading), this.fog !== void 0 && (i.fog = this.fog), Object.keys(this.userData).length > 0 && (i.userData = this.userData);
    function n(s) {
      const a = [];
      for (const o in s) {
        const l = s[o];
        delete l.metadata, a.push(l);
      }
      return a;
    }
    if (t) {
      const s = n(e.textures), a = n(e.images);
      s.length > 0 && (i.textures = s), a.length > 0 && (i.images = a);
    }
    return i;
  }
  fromJSON(e, t) {
    if (e.uuid !== void 0 && (this.uuid = e.uuid), e.name !== void 0 && (this.name = e.name), e.color !== void 0 && this.color !== void 0 && this.color.setHex(e.color), e.roughness !== void 0 && (this.roughness = e.roughness), e.metalness !== void 0 && (this.metalness = e.metalness), e.sheen !== void 0 && (this.sheen = e.sheen), e.sheenColor !== void 0 && (this.sheenColor = new be().setHex(e.sheenColor)), e.sheenRoughness !== void 0 && (this.sheenRoughness = e.sheenRoughness), e.emissive !== void 0 && this.emissive !== void 0 && this.emissive.setHex(e.emissive), e.specular !== void 0 && this.specular !== void 0 && this.specular.setHex(e.specular), e.specularIntensity !== void 0 && (this.specularIntensity = e.specularIntensity), e.specularColor !== void 0 && this.specularColor !== void 0 && this.specularColor.setHex(e.specularColor), e.shininess !== void 0 && (this.shininess = e.shininess), e.clearcoat !== void 0 && (this.clearcoat = e.clearcoat), e.clearcoatRoughness !== void 0 && (this.clearcoatRoughness = e.clearcoatRoughness), e.dispersion !== void 0 && (this.dispersion = e.dispersion), e.retroreflectivity !== void 0 && (this.retroreflectivity = e.retroreflectivity), e.iridescence !== void 0 && (this.iridescence = e.iridescence), e.iridescenceIOR !== void 0 && (this.iridescenceIOR = e.iridescenceIOR), e.iridescenceThicknessRange !== void 0 && (this.iridescenceThicknessRange = e.iridescenceThicknessRange), e.transmission !== void 0 && (this.transmission = e.transmission), e.thickness !== void 0 && (this.thickness = e.thickness), e.attenuationDistance !== void 0 && (this.attenuationDistance = e.attenuationDistance), e.attenuationColor !== void 0 && this.attenuationColor !== void 0 && this.attenuationColor.setHex(e.attenuationColor), e.anisotropy !== void 0 && (this.anisotropy = e.anisotropy), e.anisotropyRotation !== void 0 && (this.anisotropyRotation = e.anisotropyRotation), e.fog !== void 0 && (this.fog = e.fog), e.flatShading !== void 0 && (this.flatShading = e.flatShading), e.blending !== void 0 && (this.blending = e.blending), e.combine !== void 0 && (this.combine = e.combine), e.side !== void 0 && (this.side = e.side), e.shadowSide !== void 0 && (this.shadowSide = e.shadowSide), e.opacity !== void 0 && (this.opacity = e.opacity), e.transparent !== void 0 && (this.transparent = e.transparent), e.alphaTest !== void 0 && (this.alphaTest = e.alphaTest), e.alphaHash !== void 0 && (this.alphaHash = e.alphaHash), e.depthFunc !== void 0 && (this.depthFunc = e.depthFunc), e.depthTest !== void 0 && (this.depthTest = e.depthTest), e.depthWrite !== void 0 && (this.depthWrite = e.depthWrite), e.colorWrite !== void 0 && (this.colorWrite = e.colorWrite), e.clippingPlanes !== void 0 && (this.clippingPlanes = e.clippingPlanes.map((i) => new ur().fromJSON(i))), e.clipIntersection !== void 0 && (this.clipIntersection = e.clipIntersection), e.clipShadows !== void 0 && (this.clipShadows = e.clipShadows), e.depthPacking !== void 0 && (this.depthPacking = e.depthPacking), e.blendSrc !== void 0 && (this.blendSrc = e.blendSrc), e.blendDst !== void 0 && (this.blendDst = e.blendDst), e.blendEquation !== void 0 && (this.blendEquation = e.blendEquation), e.blendSrcAlpha !== void 0 && (this.blendSrcAlpha = e.blendSrcAlpha), e.blendDstAlpha !== void 0 && (this.blendDstAlpha = e.blendDstAlpha), e.blendEquationAlpha !== void 0 && (this.blendEquationAlpha = e.blendEquationAlpha), e.blendColor !== void 0 && this.blendColor !== void 0 && this.blendColor.setHex(e.blendColor), e.blendAlpha !== void 0 && (this.blendAlpha = e.blendAlpha), e.stencilWriteMask !== void 0 && (this.stencilWriteMask = e.stencilWriteMask), e.stencilFunc !== void 0 && (this.stencilFunc = e.stencilFunc), e.stencilRef !== void 0 && (this.stencilRef = e.stencilRef), e.stencilFuncMask !== void 0 && (this.stencilFuncMask = e.stencilFuncMask), e.stencilFail !== void 0 && (this.stencilFail = e.stencilFail), e.stencilZFail !== void 0 && (this.stencilZFail = e.stencilZFail), e.stencilZPass !== void 0 && (this.stencilZPass = e.stencilZPass), e.stencilWrite !== void 0 && (this.stencilWrite = e.stencilWrite), e.wireframe !== void 0 && (this.wireframe = e.wireframe), e.wireframeLinewidth !== void 0 && (this.wireframeLinewidth = e.wireframeLinewidth), e.wireframeLinecap !== void 0 && (this.wireframeLinecap = e.wireframeLinecap), e.wireframeLinejoin !== void 0 && (this.wireframeLinejoin = e.wireframeLinejoin), e.rotation !== void 0 && (this.rotation = e.rotation), e.linewidth !== void 0 && (this.linewidth = e.linewidth), e.linecap !== void 0 && (this.linecap = e.linecap), e.linejoin !== void 0 && (this.linejoin = e.linejoin), e.dashSize !== void 0 && (this.dashSize = e.dashSize), e.gapSize !== void 0 && (this.gapSize = e.gapSize), e.scale !== void 0 && (this.scale = e.scale), e.polygonOffset !== void 0 && (this.polygonOffset = e.polygonOffset), e.polygonOffsetFactor !== void 0 && (this.polygonOffsetFactor = e.polygonOffsetFactor), e.polygonOffsetUnits !== void 0 && (this.polygonOffsetUnits = e.polygonOffsetUnits), e.dithering !== void 0 && (this.dithering = e.dithering), e.alphaToCoverage !== void 0 && (this.alphaToCoverage = e.alphaToCoverage), e.premultipliedAlpha !== void 0 && (this.premultipliedAlpha = e.premultipliedAlpha), e.forceSinglePass !== void 0 && (this.forceSinglePass = e.forceSinglePass), e.allowOverride !== void 0 && (this.allowOverride = e.allowOverride), e.visible !== void 0 && (this.visible = e.visible), e.toneMapped !== void 0 && (this.toneMapped = e.toneMapped), e.userData !== void 0 && (this.userData = e.userData), e.vertexColors !== void 0 && (typeof e.vertexColors == "number" ? this.vertexColors = e.vertexColors > 0 : this.vertexColors = e.vertexColors), e.size !== void 0 && (this.size = e.size), e.sizeAttenuation !== void 0 && (this.sizeAttenuation = e.sizeAttenuation), e.map !== void 0 && (this.map = t[e.map] || null), e.matcap !== void 0 && (this.matcap = t[e.matcap] || null), e.alphaMap !== void 0 && (this.alphaMap = t[e.alphaMap] || null), e.bumpMap !== void 0 && (this.bumpMap = t[e.bumpMap] || null), e.bumpScale !== void 0 && (this.bumpScale = e.bumpScale), e.normalMap !== void 0 && (this.normalMap = t[e.normalMap] || null), e.normalMapType !== void 0 && (this.normalMapType = e.normalMapType), e.normalScale !== void 0) {
      let i = e.normalScale;
      Array.isArray(i) === !1 && (i = [i, i]), this.normalScale = new We().fromArray(i);
    }
    return e.displacementMap !== void 0 && (this.displacementMap = t[e.displacementMap] || null), e.displacementScale !== void 0 && (this.displacementScale = e.displacementScale), e.displacementBias !== void 0 && (this.displacementBias = e.displacementBias), e.roughnessMap !== void 0 && (this.roughnessMap = t[e.roughnessMap] || null), e.metalnessMap !== void 0 && (this.metalnessMap = t[e.metalnessMap] || null), e.emissiveMap !== void 0 && (this.emissiveMap = t[e.emissiveMap] || null), e.emissiveIntensity !== void 0 && (this.emissiveIntensity = e.emissiveIntensity), e.specularMap !== void 0 && (this.specularMap = t[e.specularMap] || null), e.specularIntensityMap !== void 0 && (this.specularIntensityMap = t[e.specularIntensityMap] || null), e.specularColorMap !== void 0 && (this.specularColorMap = t[e.specularColorMap] || null), e.envMap !== void 0 && (this.envMap = t[e.envMap] || null), e.envMapRotation !== void 0 && this.envMapRotation.fromArray(e.envMapRotation), e.envMapIntensity !== void 0 && (this.envMapIntensity = e.envMapIntensity), e.reflectivity !== void 0 && (this.reflectivity = e.reflectivity), e.refractionRatio !== void 0 && (this.refractionRatio = e.refractionRatio), e.lightMap !== void 0 && (this.lightMap = t[e.lightMap] || null), e.lightMapIntensity !== void 0 && (this.lightMapIntensity = e.lightMapIntensity), e.aoMap !== void 0 && (this.aoMap = t[e.aoMap] || null), e.aoMapIntensity !== void 0 && (this.aoMapIntensity = e.aoMapIntensity), e.gradientMap !== void 0 && (this.gradientMap = t[e.gradientMap] || null), e.clearcoatMap !== void 0 && (this.clearcoatMap = t[e.clearcoatMap] || null), e.clearcoatRoughnessMap !== void 0 && (this.clearcoatRoughnessMap = t[e.clearcoatRoughnessMap] || null), e.clearcoatNormalMap !== void 0 && (this.clearcoatNormalMap = t[e.clearcoatNormalMap] || null), e.clearcoatNormalScale !== void 0 && (this.clearcoatNormalScale = new We().fromArray(e.clearcoatNormalScale)), e.iridescenceMap !== void 0 && (this.iridescenceMap = t[e.iridescenceMap] || null), e.iridescenceThicknessMap !== void 0 && (this.iridescenceThicknessMap = t[e.iridescenceThicknessMap] || null), e.transmissionMap !== void 0 && (this.transmissionMap = t[e.transmissionMap] || null), e.thicknessMap !== void 0 && (this.thicknessMap = t[e.thicknessMap] || null), e.anisotropyMap !== void 0 && (this.anisotropyMap = t[e.anisotropyMap] || null), e.sheenColorMap !== void 0 && (this.sheenColorMap = t[e.sheenColorMap] || null), e.sheenRoughnessMap !== void 0 && (this.sheenRoughnessMap = t[e.sheenRoughnessMap] || null), this;
  }
  clone() {
    return new this.constructor().copy(this);
  }
  copy(e) {
    this.name = e.name, this.blending = e.blending, this.side = e.side, this.vertexColors = e.vertexColors, this.opacity = e.opacity, this.transparent = e.transparent, this.blendSrc = e.blendSrc, this.blendDst = e.blendDst, this.blendEquation = e.blendEquation, this.blendSrcAlpha = e.blendSrcAlpha, this.blendDstAlpha = e.blendDstAlpha, this.blendEquationAlpha = e.blendEquationAlpha, this.blendColor.copy(e.blendColor), this.blendAlpha = e.blendAlpha, this.depthFunc = e.depthFunc, this.depthTest = e.depthTest, this.depthWrite = e.depthWrite, this.stencilWriteMask = e.stencilWriteMask, this.stencilFunc = e.stencilFunc, this.stencilRef = e.stencilRef, this.stencilFuncMask = e.stencilFuncMask, this.stencilFail = e.stencilFail, this.stencilZFail = e.stencilZFail, this.stencilZPass = e.stencilZPass, this.stencilWrite = e.stencilWrite;
    const t = e.clippingPlanes;
    let i = null;
    if (t !== null) {
      const n = t.length;
      i = new Array(n);
      for (let s = 0; s !== n; ++s) i[s] = t[s].clone();
    }
    return this.clippingPlanes = i, this.clipIntersection = e.clipIntersection, this.clipShadows = e.clipShadows, this.shadowSide = e.shadowSide, this.colorWrite = e.colorWrite, this.precision = e.precision, this.polygonOffset = e.polygonOffset, this.polygonOffsetFactor = e.polygonOffsetFactor, this.polygonOffsetUnits = e.polygonOffsetUnits, this.dithering = e.dithering, this.alphaTest = e.alphaTest, this.alphaHash = e.alphaHash, this.alphaToCoverage = e.alphaToCoverage, this.premultipliedAlpha = e.premultipliedAlpha, this.forceSinglePass = e.forceSinglePass, this.allowOverride = e.allowOverride, this.visible = e.visible, this.toneMapped = e.toneMapped, this.userData = JSON.parse(JSON.stringify(e.userData)), this;
  }
  dispose() {
    this.dispatchEvent({ type: "dispose" });
  }
  set needsUpdate(e) {
    e === !0 && this.version++;
  }
}
const Xi = new P(), Ja = new P(), Ts = new P(), Es = new P();
class Ma {
  constructor(e = new P(), t = new P(0, 0, -1)) {
    this.origin = e, this.direction = t;
  }
  set(e, t) {
    return this.origin.copy(e), this.direction.copy(t), this;
  }
  copy(e) {
    return this.origin.copy(e.origin), this.direction.copy(e.direction), this;
  }
  at(e, t) {
    return t.copy(this.origin).addScaledVector(this.direction, e);
  }
  lookAt(e) {
    return this.direction.copy(e).sub(this.origin).normalize(), this;
  }
  recast(e) {
    return this.origin.copy(this.at(e, Xi)), this;
  }
  closestPointToPoint(e, t) {
    t.subVectors(e, this.origin);
    const i = t.dot(this.direction);
    return i < 0 ? t.copy(this.origin) : t.copy(this.origin).addScaledVector(this.direction, i);
  }
  distanceToPoint(e) {
    return Math.sqrt(this.distanceSqToPoint(e));
  }
  distanceSqToPoint(e) {
    const t = Xi.subVectors(e, this.origin).dot(this.direction);
    return t < 0 ? this.origin.distanceToSquared(e) : (Xi.copy(this.origin).addScaledVector(this.direction, t), Xi.distanceToSquared(e));
  }
  distanceSqToSegment(e, t, i, n) {
    Ja.copy(e).add(t).multiplyScalar(0.5), Ts.copy(t).sub(e).normalize(), Es.copy(this.origin).sub(Ja);
    const s = e.distanceTo(t) * 0.5, a = -this.direction.dot(Ts), o = Es.dot(this.direction), l = -Es.dot(Ts), c = Es.lengthSq(), h = Math.abs(1 - a * a);
    let d, u, p, f;
    if (h > 0) if (d = a * l - o, u = a * o - l, f = s * h, d >= 0) if (u >= -f) if (u <= f) {
      const v = 1 / h;
      d *= v, u *= v, p = d * (d + a * u + 2 * o) + u * (a * d + u + 2 * l) + c;
    } else u = s, d = Math.max(0, -(a * u + o)), p = -d * d + u * (u + 2 * l) + c;
    else u = -s, d = Math.max(0, -(a * u + o)), p = -d * d + u * (u + 2 * l) + c;
    else u <= -f ? (d = Math.max(0, -(-a * s + o)), u = d > 0 ? -s : Math.min(Math.max(-s, -l), s), p = -d * d + u * (u + 2 * l) + c) : u <= f ? (d = 0, u = Math.min(Math.max(-s, -l), s), p = u * (u + 2 * l) + c) : (d = Math.max(0, -(a * s + o)), u = d > 0 ? s : Math.min(Math.max(-s, -l), s), p = -d * d + u * (u + 2 * l) + c);
    else u = a > 0 ? -s : s, d = Math.max(0, -(a * u + o)), p = -d * d + u * (u + 2 * l) + c;
    return i && i.copy(this.origin).addScaledVector(this.direction, d), n && n.copy(Ja).addScaledVector(Ts, u), p;
  }
  intersectSphere(e, t) {
    if (e.radius < 0) return null;
    Xi.subVectors(e.center, this.origin);
    const i = Xi.dot(this.direction), n = Xi.dot(Xi) - i * i, s = e.radius * e.radius;
    if (n > s) return null;
    const a = Math.sqrt(s - n), o = i - a, l = i + a;
    return l < 0 ? null : o < 0 ? this.at(l, t) : this.at(o, t);
  }
  intersectsSphere(e) {
    return e.radius < 0 ? !1 : this.distanceSqToPoint(e.center) <= e.radius * e.radius;
  }
  distanceToPlane(e) {
    const t = e.normal.dot(this.direction);
    if (t === 0) return e.distanceToPoint(this.origin) === 0 ? 0 : null;
    const i = -(this.origin.dot(e.normal) + e.constant) / t;
    return i >= 0 ? i : null;
  }
  intersectPlane(e, t) {
    const i = this.distanceToPlane(e);
    return i === null ? null : this.at(i, t);
  }
  intersectsPlane(e) {
    const t = e.distanceToPoint(this.origin);
    return t === 0 || e.normal.dot(this.direction) * t < 0;
  }
  intersectBox(e, t) {
    let i, n, s, a, o, l;
    const c = 1 / this.direction.x, h = 1 / this.direction.y, d = 1 / this.direction.z, u = this.origin;
    return c >= 0 ? (i = (e.min.x - u.x) * c, n = (e.max.x - u.x) * c) : (i = (e.max.x - u.x) * c, n = (e.min.x - u.x) * c), h >= 0 ? (s = (e.min.y - u.y) * h, a = (e.max.y - u.y) * h) : (s = (e.max.y - u.y) * h, a = (e.min.y - u.y) * h), i > a || s > n || ((s > i || isNaN(i)) && (i = s), (a < n || isNaN(n)) && (n = a), d >= 0 ? (o = (e.min.z - u.z) * d, l = (e.max.z - u.z) * d) : (o = (e.max.z - u.z) * d, l = (e.min.z - u.z) * d), i > l || o > n) || ((o > i || i !== i) && (i = o), (l < n || n !== n) && (n = l), n < 0) ? null : this.at(i >= 0 ? i : n, t);
  }
  intersectsBox(e) {
    return this.intersectBox(e, Xi) !== null;
  }
  intersectTriangle(e, t, i, n, s) {
    const a = this.origin, o = this.direction, l = o.x, c = o.y, h = o.z, d = e.x - a.x, u = e.y - a.y, p = e.z - a.z, f = t.x - a.x, v = t.y - a.y, m = t.z - a.z, g = i.x - a.x, E = i.y - a.y, R = i.z - a.z, y = Math.abs(l), S = Math.abs(c), T = Math.abs(h);
    let A, x, b, N, O, k, q, B, G, Z, X, te;
    if (y >= S && y >= T ? (b = l, k = d, G = f, te = g, l >= 0 ? (A = c, x = h, N = u, O = p, q = v, B = m, Z = E, X = R) : (A = h, x = c, N = p, O = u, q = m, B = v, Z = R, X = E)) : S >= T ? (b = c, k = u, G = v, te = E, c >= 0 ? (A = h, x = l, N = p, O = d, q = m, B = f, Z = R, X = g) : (A = l, x = h, N = d, O = p, q = f, B = m, Z = g, X = R)) : (b = h, k = p, G = m, te = R, h >= 0 ? (A = l, x = c, N = d, O = u, q = f, B = v, Z = g, X = E) : (A = c, x = l, N = u, O = d, q = v, B = f, Z = E, X = g)), b === 0) return null;
    const V = A / b, K = x / b, ee = 1 / b, Ue = N - V * k, ue = O - K * k, Ye = q - V * G, ke = B - K * G, je = Z - V * te, Y = X - K * te, $ = je * ke - Y * Ye, le = Ue * Y - ue * je, Oe = Ye * ue - ke * Ue;
    if (n) {
      if ($ < 0 || le < 0 || Oe < 0) return null;
    } else if (($ < 0 || le < 0 || Oe < 0) && ($ > 0 || le > 0 || Oe > 0)) return null;
    const xe = $ + le + Oe;
    if (xe === 0) return null;
    const Te = ee * ($ * k + le * G + Oe * te);
    return (xe > 0 ? Te < 0 : Te > 0) ? null : this.at(Te / xe, s);
  }
  applyMatrix4(e) {
    return this.origin.applyMatrix4(e), this.direction.transformDirection(e), this;
  }
  equals(e) {
    return e.origin.equals(this.origin) && e.direction.equals(this.direction);
  }
  clone() {
    return new this.constructor().copy(this);
  }
}
class Ki extends vi {
  constructor(e) {
    super(), this.isMeshBasicMaterial = !0, this.type = "MeshBasicMaterial", this.color = new be(16777215), this.map = null, this.lightMap = null, this.lightMapIntensity = 1, this.aoMap = null, this.aoMapIntensity = 1, this.specularMap = null, this.alphaMap = null, this.envMap = null, this.envMapRotation = new Yt(), this.combine = Tu, this.reflectivity = 1, this.refractionRatio = 0.98, this.wireframe = !1, this.wireframeLinewidth = 1, this.wireframeLinecap = "round", this.wireframeLinejoin = "round", this.fog = !0, this.setValues(e);
  }
  copy(e) {
    return super.copy(e), this.color.copy(e.color), this.map = e.map, this.lightMap = e.lightMap, this.lightMapIntensity = e.lightMapIntensity, this.aoMap = e.aoMap, this.aoMapIntensity = e.aoMapIntensity, this.specularMap = e.specularMap, this.alphaMap = e.alphaMap, this.envMap = e.envMap, this.envMapRotation.copy(e.envMapRotation), this.combine = e.combine, this.reflectivity = e.reflectivity, this.refractionRatio = e.refractionRatio, this.wireframe = e.wireframe, this.wireframeLinewidth = e.wireframeLinewidth, this.wireframeLinecap = e.wireframeLinecap, this.wireframeLinejoin = e.wireframeLinejoin, this.fog = e.fog, this;
  }
}
const Rc = new Fe(), Tr = new Ma(), bs = new Di(), Pc = new P(), ws = new P(), As = new P(), Rs = new P(), eo = new P(), Ps = new P(), Cc = new P(), Cs = new P();
class Wt extends at {
  constructor(e = new yt(), t = new Ki()) {
    super(), this.isMesh = !0, this.type = "Mesh", this.geometry = e, this.material = t, this.morphTargetDictionary = void 0, this.morphTargetInfluences = void 0, this.count = 1, this.updateMorphTargets();
  }
  copy(e, t) {
    return super.copy(e, t), e.morphTargetInfluences !== void 0 && (this.morphTargetInfluences = e.morphTargetInfluences.slice()), e.morphTargetDictionary !== void 0 && (this.morphTargetDictionary = Object.assign({}, e.morphTargetDictionary)), this.material = Array.isArray(e.material) ? e.material.slice() : e.material, this.geometry = e.geometry, this;
  }
  updateMorphTargets() {
    const e = this.geometry.morphAttributes, t = Object.keys(e);
    if (t.length > 0) {
      const i = e[t[0]];
      if (i !== void 0) {
        this.morphTargetInfluences = [], this.morphTargetDictionary = {};
        for (let n = 0, s = i.length; n < s; n++) {
          const a = i[n].name || String(n);
          this.morphTargetInfluences.push(0), this.morphTargetDictionary[a] = n;
        }
      }
    }
  }
  getVertexPosition(e, t) {
    const i = this.geometry, n = i.attributes.position, s = i.morphAttributes.position, a = i.morphTargetsRelative;
    t.fromBufferAttribute(n, e);
    const o = this.morphTargetInfluences;
    if (s && o) {
      Ps.set(0, 0, 0);
      for (let l = 0, c = s.length; l < c; l++) {
        const h = o[l], d = s[l];
        h !== 0 && (eo.fromBufferAttribute(d, e), a ? Ps.addScaledVector(eo, h) : Ps.addScaledVector(eo.sub(t), h));
      }
      t.add(Ps);
    }
    return t;
  }
  intersectsFrustum(e) {
    return e.intersectsObject(this);
  }
  raycast(e, t) {
    const i = this.geometry, n = this.material, s = this.matrixWorld;
    n !== void 0 && (i.boundingSphere === null && i.computeBoundingSphere(), bs.copy(i.boundingSphere), bs.applyMatrix4(s), Tr.copy(e.ray).recast(e.near), !(bs.containsPoint(Tr.origin) === !1 && (Tr.intersectSphere(bs, Pc) === null || Tr.origin.distanceToSquared(Pc) > (e.far - e.near) ** 2)) && (Rc.copy(s).invert(), Tr.copy(e.ray).applyMatrix4(Rc), !(i.boundingBox !== null && Tr.intersectsBox(i.boundingBox) === !1) && this._computeIntersections(e, t, Tr)));
  }
  _computeIntersections(e, t, i) {
    let n;
    const s = this.geometry, a = this.material, o = s.index, l = s.attributes.position, c = s.attributes.uv, h = s.attributes.uv1, d = s.attributes.normal, u = s.groups, p = s.drawRange;
    if (o !== null) if (Array.isArray(a)) for (let f = 0, v = u.length; f < v; f++) {
      const m = u[f], g = a[m.materialIndex], E = Math.max(m.start, p.start), R = Math.min(o.count, Math.min(m.start + m.count, p.start + p.count));
      for (let y = E, S = R; y < S; y += 3) {
        const T = o.getX(y), A = o.getX(y + 1), x = o.getX(y + 2);
        n = Ls(this, g, e, i, c, h, d, T, A, x), n && (n.faceIndex = Math.floor(y / 3), n.face.materialIndex = m.materialIndex, t.push(n));
      }
    }
    else {
      const f = Math.max(0, p.start), v = Math.min(o.count, p.start + p.count);
      for (let m = f, g = v; m < g; m += 3) {
        const E = o.getX(m), R = o.getX(m + 1), y = o.getX(m + 2);
        n = Ls(this, a, e, i, c, h, d, E, R, y), n && (n.faceIndex = Math.floor(m / 3), t.push(n));
      }
    }
    else if (l !== void 0) if (Array.isArray(a)) for (let f = 0, v = u.length; f < v; f++) {
      const m = u[f], g = a[m.materialIndex], E = Math.max(m.start, p.start), R = Math.min(l.count, Math.min(m.start + m.count, p.start + p.count));
      for (let y = E, S = R; y < S; y += 3) {
        const T = y, A = y + 1, x = y + 2;
        n = Ls(this, g, e, i, c, h, d, T, A, x), n && (n.faceIndex = Math.floor(y / 3), n.face.materialIndex = m.materialIndex, t.push(n));
      }
    }
    else {
      const f = Math.max(0, p.start), v = Math.min(l.count, p.start + p.count);
      for (let m = f, g = v; m < g; m += 3) {
        const E = m, R = m + 1, y = m + 2;
        n = Ls(this, a, e, i, c, h, d, E, R, y), n && (n.faceIndex = Math.floor(m / 3), t.push(n));
      }
    }
  }
}
function Nf(r, e, t, i, n, s, a, o) {
  let l;
  if (e.side === Gt ? l = i.intersectTriangle(a, s, n, !0, o) : l = i.intersectTriangle(n, s, a, e.side === fr, o), l === null) return null;
  Cs.copy(o), Cs.applyMatrix4(r.matrixWorld);
  const c = t.ray.origin.distanceTo(Cs);
  return c < t.near || c > t.far ? null : { distance: c, point: Cs.clone(), object: r };
}
function Ls(r, e, t, i, n, s, a, o, l, c) {
  r.getVertexPosition(o, ws), r.getVertexPosition(l, As), r.getVertexPosition(c, Rs);
  const h = Nf(r, e, t, i, ws, As, Rs, Cc);
  if (h) {
    const d = new P();
    fi.getBarycoord(Cc, ws, As, Rs, d), n && (h.uv = fi.getInterpolatedAttribute(n, o, l, c, d, new We())), s && (h.uv1 = fi.getInterpolatedAttribute(s, o, l, c, d, new We())), a && (h.normal = fi.getInterpolatedAttribute(a, o, l, c, d, new P()), h.normal.dot(i.direction) > 0 && h.normal.multiplyScalar(-1));
    const u = { a: o, b: l, c, normal: new P(), materialIndex: 0 };
    fi.getNormal(ws, As, Rs, u.normal), h.face = u, h.barycoord = d;
  }
  return h;
}
const Nn = new gt(), Lc = new gt(), Ic = new gt(), Uf = new gt(), Nc = new Fe(), Is = new P(), to = new Di(), Uc = new Fe(), io = new Ma();
class Ju extends Wt {
  constructor(e, t) {
    super(e, t), this.isSkinnedMesh = !0, this.type = "SkinnedMesh", this.bindMode = dc, this.bindMatrix = new Fe(), this.bindMatrixInverse = new Fe(), this.boundingBox = null, this.boundingSphere = null;
  }
  computeBoundingBox() {
    const e = this.geometry;
    this.boundingBox === null && (this.boundingBox = new er()), this.boundingBox.makeEmpty();
    const t = e.getAttribute("position");
    for (let i = 0; i < t.count; i++) this.getVertexPosition(i, Is), this.boundingBox.expandByPoint(Is);
  }
  computeBoundingSphere() {
    const e = this.geometry;
    this.boundingSphere === null && (this.boundingSphere = new Di()), this.boundingSphere.makeEmpty();
    const t = e.getAttribute("position");
    for (let i = 0; i < t.count; i++) this.getVertexPosition(i, Is), this.boundingSphere.expandByPoint(Is);
  }
  copy(e, t) {
    return super.copy(e, t), this.bindMode = e.bindMode, this.bindMatrix.copy(e.bindMatrix), this.bindMatrixInverse.copy(e.bindMatrixInverse), this.skeleton = e.skeleton, e.boundingBox !== null && (this.boundingBox = e.boundingBox.clone()), e.boundingSphere !== null && (this.boundingSphere = e.boundingSphere.clone()), this;
  }
  raycast(e, t) {
    const i = this.material, n = this.matrixWorld;
    i !== void 0 && (this.boundingSphere === null && this.computeBoundingSphere(), to.copy(this.boundingSphere), to.applyMatrix4(n), e.ray.intersectsSphere(to) !== !1 && (Uc.copy(n).invert(), io.copy(e.ray).applyMatrix4(Uc), !(this.boundingBox !== null && io.intersectsBox(this.boundingBox) === !1) && this._computeIntersections(e, t, io)));
  }
  getVertexPosition(e, t) {
    return super.getVertexPosition(e, t), this.applyBoneTransform(e, t), t;
  }
  bind(e, t) {
    this.skeleton = e, t === void 0 && (this.updateMatrixWorld(!0), this.skeleton.calculateInverses(), t = this.matrixWorld), this.bindMatrix.copy(t), this.bindMatrixInverse.copy(t).invert();
  }
  pose() {
    this.skeleton.pose();
  }
  normalizeSkinWeights() {
    const e = new gt(), t = this.geometry.attributes.skinWeight;
    for (let i = 0, n = t.count; i < n; i++) {
      e.fromBufferAttribute(t, i);
      const s = 1 / e.manhattanLength();
      s !== 1 / 0 ? e.multiplyScalar(s) : e.set(1, 0, 0, 0), t.setXYZW(i, e.x, e.y, e.z, e.w);
    }
  }
  updateMatrixWorld(e) {
    super.updateMatrixWorld(e), this.bindMode === dc ? this.bindMatrixInverse.copy(this.matrixWorld).invert() : this.bindMode === Lp ? this.bindMatrixInverse.copy(this.bindMatrix).invert() : Re("SkinnedMesh: Unrecognized bindMode: " + this.bindMode);
  }
  applyBoneTransform(e, t) {
    const i = this.skeleton, n = this.geometry;
    Lc.fromBufferAttribute(n.attributes.skinIndex, e), Ic.fromBufferAttribute(n.attributes.skinWeight, e), t.isVector4 ? (Nn.copy(t), t.set(0, 0, 0, 0)) : (Nn.set(...t, 1), t.set(0, 0, 0)), Nn.applyMatrix4(this.bindMatrix);
    for (let s = 0; s < 4; s++) {
      const a = Ic.getComponent(s);
      if (a !== 0) {
        const o = Lc.getComponent(s);
        Nc.multiplyMatrices(i.bones[o].matrixWorld, i.boneInverses[o]), t.addScaledVector(Uf.copy(Nn).applyMatrix4(Nc), a);
      }
    }
    return t.isVector4 && (t.w = Nn.w), t.applyMatrix4(this.bindMatrixInverse);
  }
}
class ed extends at {
  constructor() {
    super(), this.isBone = !0, this.type = "Bone";
  }
}
class Yl extends Nt {
  constructor(e = null, t = 1, i = 1, n, s, a, o, l, c = Rt, h = Rt, d, u) {
    super(null, a, o, l, c, h, n, s, d, u), this.isDataTexture = !0, this.image = { data: e, width: t, height: i }, this.generateMipmaps = !1, this.flipY = !1, this.unpackAlignment = 1;
  }
}
const Dc = new Fe(), Df = new Fe();
class Sn {
  constructor(e = [], t = []) {
    this.uuid = gi(), this.bones = e.slice(0), this.boneInverses = t, this.boneMatrices = null, this.boneTexture = null, this.init();
  }
  init() {
    const e = this.bones, t = this.boneInverses;
    if (this.boneMatrices = new Float32Array(e.length * 16), t.length === 0) this.calculateInverses();
    else if (e.length !== t.length) {
      Re("Skeleton: Number of inverse bone matrices does not match amount of bones."), this.boneInverses = [];
      for (let i = 0, n = this.bones.length; i < n; i++) this.boneInverses.push(new Fe());
    }
  }
  calculateInverses() {
    this.boneInverses.length = 0;
    for (let e = 0, t = this.bones.length; e < t; e++) {
      const i = new Fe();
      this.bones[e] && i.copy(this.bones[e].matrixWorld).invert(), this.boneInverses.push(i);
    }
  }
  pose() {
    for (let e = 0, t = this.bones.length; e < t; e++) {
      const i = this.bones[e];
      i && i.matrixWorld.copy(this.boneInverses[e]).invert();
    }
    for (let e = 0, t = this.bones.length; e < t; e++) {
      const i = this.bones[e];
      i && (i.parent && i.parent.isBone ? (i.matrix.copy(i.parent.matrixWorld).invert(), i.matrix.multiply(i.matrixWorld)) : i.matrix.copy(i.matrixWorld), i.matrix.decompose(i.position, i.quaternion, i.scale));
    }
  }
  update() {
    const e = this.bones, t = this.boneInverses, i = this.boneMatrices, n = this.boneTexture;
    for (let s = 0, a = e.length; s < a; s++) {
      const o = e[s] ? e[s].matrixWorld : Df;
      Dc.multiplyMatrices(o, t[s]), Dc.toArray(i, s * 16);
    }
    n !== null && (n.needsUpdate = !0);
  }
  clone() {
    return new Sn(this.bones, this.boneInverses);
  }
  computeBoneTexture() {
    let e = Math.sqrt(this.bones.length * 4);
    e = Math.ceil(e / 4) * 4, e = Math.max(e, 4);
    const t = new Float32Array(e * e * 4);
    t.set(this.boneMatrices);
    const i = new Yl(t, e, e, li, oi);
    return i.needsUpdate = !0, this.boneMatrices = t, this.boneTexture = i, this;
  }
  getBoneByName(e) {
    for (let t = 0, i = this.bones.length; t < i; t++) {
      const n = this.bones[t];
      if (n.name === e) return n;
    }
  }
  dispose() {
    this.boneTexture !== null && (this.boneTexture.dispose(), this.boneTexture = null);
  }
  fromJSON(e, t) {
    this.uuid = e.uuid;
    for (let i = 0, n = e.bones.length; i < n; i++) {
      const s = e.bones[i];
      let a = t[s];
      a === void 0 && (Re("Skeleton: No bone found with UUID:", s), a = new ed()), this.bones.push(a), this.boneInverses.push(new Fe().fromArray(e.boneInverses[i]));
    }
    return this.init(), this;
  }
  toJSON() {
    const e = { metadata: { version: 4.7, type: "Skeleton", generator: "Skeleton.toJSON" }, bones: [], boneInverses: [] };
    e.uuid = this.uuid;
    const t = this.bones, i = this.boneInverses;
    for (let n = 0, s = t.length; n < s; n++) {
      const a = t[n];
      e.bones.push(a.uuid);
      const o = i[n];
      e.boneInverses.push(o.toArray());
    }
    return e;
  }
}
class ua extends nt {
  constructor(e, t, i, n = 1) {
    super(e, t, i), this.isInstancedBufferAttribute = !0, this.meshPerAttribute = n;
  }
  copy(e) {
    return super.copy(e), this.meshPerAttribute = e.meshPerAttribute, this;
  }
  toJSON() {
    const e = super.toJSON();
    return e.meshPerAttribute = this.meshPerAttribute, e.isInstancedBufferAttribute = !0, e;
  }
}
const Yr = new Fe(), Oc = new Fe(), Ns = [], Fc = new er(), Of = new Fe(), Un = new Wt(), Dn = new Di();
class Ff extends Wt {
  constructor(e, t, i) {
    super(e, t), this.isInstancedMesh = !0, this.instanceMatrix = new ua(new Float32Array(i * 16), 16), this.instanceColor = null, this.morphTexture = null, this.count = i, this.boundingBox = null, this.boundingSphere = null;
    for (let n = 0; n < i; n++) this.setMatrixAt(n, Of);
  }
  computeBoundingBox() {
    const e = this.geometry, t = this.count;
    this.boundingBox === null && (this.boundingBox = new er()), e.boundingBox === null && e.computeBoundingBox(), this.boundingBox.makeEmpty();
    for (let i = 0; i < t; i++) this.getMatrixAt(i, Yr), Fc.copy(e.boundingBox).applyMatrix4(Yr), this.boundingBox.union(Fc);
  }
  computeBoundingSphere() {
    const e = this.geometry, t = this.count;
    this.boundingSphere === null && (this.boundingSphere = new Di()), e.boundingSphere === null && e.computeBoundingSphere(), this.boundingSphere.makeEmpty();
    for (let i = 0; i < t; i++) this.getMatrixAt(i, Yr), Dn.copy(e.boundingSphere).applyMatrix4(Yr), this.boundingSphere.union(Dn);
  }
  copy(e, t) {
    return super.copy(e, t), this.instanceMatrix.copy(e.instanceMatrix), e.morphTexture !== null && (this.morphTexture = e.morphTexture.clone()), e.instanceColor !== null && (this.instanceColor = e.instanceColor.clone()), this.count = e.count, e.boundingBox !== null && (this.boundingBox = e.boundingBox.clone()), e.boundingSphere !== null && (this.boundingSphere = e.boundingSphere.clone()), this;
  }
  getColorAt(e, t) {
    return this.instanceColor === null ? t.setRGB(1, 1, 1) : t.fromArray(this.instanceColor.array, e * 3);
  }
  getMatrixAt(e, t) {
    return t.fromArray(this.instanceMatrix.array, e * 16);
  }
  getMorphAt(e, t) {
    const i = t.morphTargetInfluences, n = this.morphTexture.source.data.data, s = i.length + 1, a = e * s + 1;
    for (let o = 0; o < i.length; o++) i[o] = n[a + o];
  }
  raycast(e, t) {
    const i = this.matrixWorld, n = this.count;
    if (Un.geometry = this.geometry, Un.material = this.material, Un.material !== void 0 && (this.boundingSphere === null && this.computeBoundingSphere(), Dn.copy(this.boundingSphere), Dn.applyMatrix4(i), e.ray.intersectsSphere(Dn) !== !1)) for (let s = 0; s < n; s++) {
      this.getMatrixAt(s, Yr), Oc.multiplyMatrices(i, Yr), Un.matrixWorld = Oc, Un.raycast(e, Ns);
      for (let a = 0, o = Ns.length; a < o; a++) {
        const l = Ns[a];
        l.instanceId = s, l.object = this, t.push(l);
      }
      Ns.length = 0;
    }
  }
  setColorAt(e, t) {
    return this.instanceColor === null && (this.instanceColor = new ua(new Float32Array(this.instanceMatrix.count * 3).fill(1), 3)), t.toArray(this.instanceColor.array, e * 3), this;
  }
  setMatrixAt(e, t) {
    return t.toArray(this.instanceMatrix.array, e * 16), this;
  }
  setMorphAt(e, t) {
    const i = t.morphTargetInfluences, n = i.length + 1;
    this.morphTexture === null && (this.morphTexture = new Yl(new Float32Array(n * this.count), n, this.count, Bl, oi));
    const s = this.morphTexture.source.data.data;
    let a = 0;
    for (let c = 0; c < i.length; c++) a += i[c];
    const o = this.geometry.morphTargetsRelative ? 1 : 1 - a, l = n * e;
    return s[l] = o, s.set(i, l + 1), this;
  }
  updateMorphTargets() {
  }
  dispose() {
    super.dispose(), this.morphTexture !== null && (this.morphTexture.dispose(), this.morphTexture = null);
  }
}
const Er = new Di(), Bf = new We(0.5, 0.5), Us = new P();
class Kl {
  constructor(e = new ur(), t = new ur(), i = new ur(), n = new ur(), s = new ur(), a = new ur()) {
    this.planes = [e, t, i, n, s, a];
  }
  set(e, t, i, n, s, a) {
    const o = this.planes;
    return o[0].copy(e), o[1].copy(t), o[2].copy(i), o[3].copy(n), o[4].copy(s), o[5].copy(a), this;
  }
  copy(e) {
    const t = this.planes;
    for (let i = 0; i < 6; i++) t[i].copy(e.planes[i]);
    return this;
  }
  setFromProjectionMatrix(e, t = Li, i = !1) {
    const n = this.planes, s = e.elements, a = s[0], o = s[1], l = s[2], c = s[3], h = s[4], d = s[5], u = s[6], p = s[7], f = s[8], v = s[9], m = s[10], g = s[11], E = s[12], R = s[13], y = s[14], S = s[15];
    if (n[0].setComponents(c - a, p - h, g - f, S - E).normalize(), n[1].setComponents(c + a, p + h, g + f, S + E).normalize(), n[2].setComponents(c + o, p + d, g + v, S + R).normalize(), n[3].setComponents(c - o, p - d, g - v, S - R).normalize(), i) n[4].setComponents(l, u, m, y).normalize(), n[5].setComponents(c - l, p - u, g - m, S - y).normalize();
    else if (n[4].setComponents(c - l, p - u, g - m, S - y).normalize(), t === Li) n[5].setComponents(c + l, p + u, g + m, S + y).normalize();
    else if (t === os) n[5].setComponents(l, u, m, y).normalize();
    else throw new Error("THREE.Frustum.setFromProjectionMatrix(): Invalid coordinate system: " + t);
    return this;
  }
  intersectsObject(e) {
    if (e.boundingSphere !== void 0) e.boundingSphere === null && e.computeBoundingSphere(), Er.copy(e.boundingSphere).applyMatrix4(e.matrixWorld);
    else {
      const t = e.geometry;
      t.boundingSphere === null && t.computeBoundingSphere(), Er.copy(t.boundingSphere).applyMatrix4(e.matrixWorld);
    }
    return this.intersectsSphere(Er);
  }
  intersectsSprite(e) {
    Er.center.set(0, 0, 0);
    const t = Bf.distanceTo(e.center);
    return Er.radius = 0.7071067811865476 + t, Er.applyMatrix4(e.matrixWorld), this.intersectsSphere(Er);
  }
  intersectsSphere(e) {
    const t = this.planes, i = e.center, n = -e.radius;
    for (let s = 0; s < 6; s++) if (t[s].distanceToPoint(i) < n) return !1;
    return !0;
  }
  intersectsBox(e) {
    const t = this.planes;
    for (let i = 0; i < 6; i++) {
      const n = t[i];
      if (Us.x = n.normal.x > 0 ? e.max.x : e.min.x, Us.y = n.normal.y > 0 ? e.max.y : e.min.y, Us.z = n.normal.z > 0 ? e.max.z : e.min.z, n.distanceToPoint(Us) < 0) return !1;
    }
    return !0;
  }
  containsPoint(e) {
    const t = this.planes;
    for (let i = 0; i < 6; i++) if (t[i].distanceToPoint(e) < 0) return !1;
    return !0;
  }
  clone() {
    return new this.constructor().copy(this);
  }
}
class Dr extends vi {
  constructor(e) {
    super(), this.isLineBasicMaterial = !0, this.type = "LineBasicMaterial", this.color = new be(16777215), this.map = null, this.linewidth = 1, this.linecap = "round", this.linejoin = "round", this.fog = !0, this.setValues(e);
  }
  copy(e) {
    return super.copy(e), this.color.copy(e.color), this.map = e.map, this.linewidth = e.linewidth, this.linecap = e.linecap, this.linejoin = e.linejoin, this.fog = e.fog, this;
  }
}
const da = new P(), pa = new P(), Bc = new Fe(), On = new Ma(), Ds = new Di(), ro = new P(), Vc = new P();
class ya extends at {
  constructor(e = new yt(), t = new Dr()) {
    super(), this.isLine = !0, this.type = "Line", this.geometry = e, this.material = t, this.morphTargetDictionary = void 0, this.morphTargetInfluences = void 0, this.updateMorphTargets();
  }
  copy(e, t) {
    return super.copy(e, t), this.material = Array.isArray(e.material) ? e.material.slice() : e.material, this.geometry = e.geometry, this;
  }
  computeLineDistances() {
    const e = this.geometry;
    if (e.index === null) {
      const t = e.attributes.position, i = [0];
      for (let n = 1, s = t.count; n < s; n++) da.fromBufferAttribute(t, n - 1), pa.fromBufferAttribute(t, n), i[n] = i[n - 1], i[n] += da.distanceTo(pa);
      e.setAttribute("lineDistance", new Jt(i, 1));
    } else Re("Line.computeLineDistances(): Computation only possible with non-indexed BufferGeometry.");
    return this;
  }
  intersectsFrustum(e) {
    return e.intersectsObject(this);
  }
  raycast(e, t) {
    const i = this.geometry, n = this.matrixWorld, s = e.params.Line.threshold, a = i.drawRange;
    if (i.boundingSphere === null && i.computeBoundingSphere(), Ds.copy(i.boundingSphere), Ds.applyMatrix4(n), Ds.radius += s, e.ray.intersectsSphere(Ds) === !1) return;
    Bc.copy(n).invert(), On.copy(e.ray).applyMatrix4(Bc);
    const o = s / ((this.scale.x + this.scale.y + this.scale.z) / 3), l = o * o, c = this.isLineSegments ? 2 : 1, h = i.index, d = i.attributes.position;
    if (h !== null) {
      const u = Math.max(0, a.start), p = Math.min(h.count, a.start + a.count);
      for (let f = u, v = p - 1; f < v; f += c) {
        const m = h.getX(f), g = h.getX(f + 1), E = Os(this, e, On, l, m, g, f);
        E && t.push(E);
      }
      if (this.isLineLoop) {
        const f = h.getX(p - 1), v = h.getX(u), m = Os(this, e, On, l, f, v, p - 1);
        m && t.push(m);
      }
    } else {
      const u = Math.max(0, a.start), p = Math.min(d.count, a.start + a.count);
      for (let f = u, v = p - 1; f < v; f += c) {
        const m = Os(this, e, On, l, f, f + 1, f);
        m && t.push(m);
      }
      if (this.isLineLoop) {
        const f = Os(this, e, On, l, p - 1, u, p - 1);
        f && t.push(f);
      }
    }
  }
  updateMorphTargets() {
    const e = this.geometry.morphAttributes, t = Object.keys(e);
    if (t.length > 0) {
      const i = e[t[0]];
      if (i !== void 0) {
        this.morphTargetInfluences = [], this.morphTargetDictionary = {};
        for (let n = 0, s = i.length; n < s; n++) {
          const a = i[n].name || String(n);
          this.morphTargetInfluences.push(0), this.morphTargetDictionary[a] = n;
        }
      }
    }
  }
}
function Os(r, e, t, i, n, s, a) {
  const o = r.geometry.attributes.position;
  if (da.fromBufferAttribute(o, n), pa.fromBufferAttribute(o, s), t.distanceSqToSegment(da, pa, ro, Vc) > i) return;
  ro.applyMatrix4(r.matrixWorld);
  const l = e.ray.origin.distanceTo(ro);
  if (!(l < e.near || l > e.far)) return { distance: l, point: Vc.clone().applyMatrix4(r.matrixWorld), index: a, face: null, faceIndex: null, barycoord: null, object: r };
}
const Hc = new P(), kc = new P();
class ds extends ya {
  constructor(e, t) {
    super(e, t), this.isLineSegments = !0, this.type = "LineSegments";
  }
  computeLineDistances() {
    const e = this.geometry;
    if (e.index === null) {
      const t = e.attributes.position, i = [];
      for (let n = 0, s = t.count; n < s; n += 2) Hc.fromBufferAttribute(t, n), kc.fromBufferAttribute(t, n + 1), i[n] = n === 0 ? 0 : i[n - 1], i[n + 1] = i[n] + Hc.distanceTo(kc);
      e.setAttribute("lineDistance", new Jt(i, 1));
    } else Re("LineSegments.computeLineDistances(): Computation only possible with non-indexed BufferGeometry.");
    return this;
  }
}
class Vf extends ya {
  constructor(e, t) {
    super(e, t), this.isLineLoop = !0, this.type = "LineLoop";
  }
}
class td extends vi {
  constructor(e) {
    super(), this.isPointsMaterial = !0, this.type = "PointsMaterial", this.color = new be(16777215), this.map = null, this.alphaMap = null, this.size = 1, this.sizeAttenuation = !0, this.fog = !0, this.setValues(e);
  }
  copy(e) {
    return super.copy(e), this.color.copy(e.color), this.map = e.map, this.alphaMap = e.alphaMap, this.size = e.size, this.sizeAttenuation = e.sizeAttenuation, this.fog = e.fog, this;
  }
}
const zc = new Fe(), Ml = new Ma(), Fs = new Di(), Bs = new P();
class Hf extends at {
  constructor(e = new yt(), t = new td()) {
    super(), this.isPoints = !0, this.type = "Points", this.geometry = e, this.material = t, this.morphTargetDictionary = void 0, this.morphTargetInfluences = void 0, this.updateMorphTargets();
  }
  copy(e, t) {
    return super.copy(e, t), this.material = Array.isArray(e.material) ? e.material.slice() : e.material, this.geometry = e.geometry, this;
  }
  intersectsFrustum(e) {
    return e.intersectsObject(this);
  }
  raycast(e, t) {
    const i = this.geometry, n = this.matrixWorld, s = e.params.Points.threshold, a = i.drawRange;
    if (i.boundingSphere === null && i.computeBoundingSphere(), Fs.copy(i.boundingSphere), Fs.applyMatrix4(n), Fs.radius += s, e.ray.intersectsSphere(Fs) === !1) return;
    zc.copy(n).invert(), Ml.copy(e.ray).applyMatrix4(zc);
    const o = s / ((this.scale.x + this.scale.y + this.scale.z) / 3), l = o * o, c = i.index, h = i.attributes.position;
    if (c !== null) {
      const d = Math.max(0, a.start), u = Math.min(c.count, a.start + a.count);
      for (let p = d, f = u; p < f; p++) {
        const v = c.getX(p);
        Bs.fromBufferAttribute(h, v), Gc(Bs, v, l, n, e, t, this);
      }
    } else {
      const d = Math.max(0, a.start), u = Math.min(h.count, a.start + a.count);
      for (let p = d, f = u; p < f; p++) Bs.fromBufferAttribute(h, p), Gc(Bs, p, l, n, e, t, this);
    }
  }
  updateMorphTargets() {
    const e = this.geometry.morphAttributes, t = Object.keys(e);
    if (t.length > 0) {
      const i = e[t[0]];
      if (i !== void 0) {
        this.morphTargetInfluences = [], this.morphTargetDictionary = {};
        for (let n = 0, s = i.length; n < s; n++) {
          const a = i[n].name || String(n);
          this.morphTargetInfluences.push(0), this.morphTargetDictionary[a] = n;
        }
      }
    }
  }
}
function Gc(r, e, t, i, n, s, a) {
  const o = Ml.distanceSqToPoint(r);
  if (o < t) {
    const l = new P();
    Ml.closestPointToPoint(r, l), l.applyMatrix4(i);
    const c = n.ray.origin.distanceTo(l);
    if (c < n.near || c > n.far) return;
    s.push({ distance: c, distanceToRay: Math.sqrt(o), point: l, index: e, face: null, faceIndex: null, barycoord: null, object: a });
  }
}
class id extends Nt {
  constructor(e = [], t = Nr, i, n, s, a, o, l, c, h) {
    super(e, t, i, n, s, a, o, l, c, h), this.isCubeTexture = !0, this.flipY = !1;
  }
  get images() {
    return this.image;
  }
  set images(e) {
    this.image = e;
  }
}
class cs extends Nt {
  constructor(e, t, i = Ni, n, s, a, o = Rt, l = Rt, c, h = Ji, d = 1) {
    if (h !== Ji && h !== Cr) throw new Error("THREE.DepthTexture: format must be either THREE.DepthFormat or THREE.DepthStencilFormat");
    const u = { width: e, height: t, depth: d };
    super(u, n, s, a, o, l, h, i, c), this.isDepthTexture = !0, this.flipY = !1, this.generateMipmaps = !1, this.compareFunction = null;
  }
  copy(e) {
    return super.copy(e), this.source = new jl(Object.assign({}, e.image)), this.compareFunction = e.compareFunction, this;
  }
  toJSON(e) {
    const t = super.toJSON(e);
    return t.compareFunction = this.compareFunction, t;
  }
}
class kf extends cs {
  constructor(e, t = Ni, i = Nr, n, s, a = Rt, o = Rt, l, c = Ji) {
    const h = { width: e, height: e, depth: 1 }, d = [h, h, h, h, h, h];
    super(e, e, t, i, n, s, a, o, l, c), this.image = d, this.isCubeDepthTexture = !0, this.isCubeTexture = !0;
  }
  get images() {
    return this.image;
  }
  set images(e) {
    this.image = e;
  }
}
class rd extends Nt {
  constructor(e = null) {
    super(), this.sourceTexture = e, this.isExternalTexture = !0;
  }
  copy(e) {
    return super.copy(e), this.sourceTexture = e.sourceTexture, this;
  }
}
class ps extends yt {
  constructor(e = 1, t = 1, i = 1, n = 1, s = 1, a = 1) {
    super(), this.type = "BoxGeometry", this.parameters = { width: e, height: t, depth: i, widthSegments: n, heightSegments: s, depthSegments: a };
    const o = this;
    n = Math.floor(n), s = Math.floor(s), a = Math.floor(a);
    const l = [], c = [], h = [], d = [];
    let u = 0, p = 0;
    f("z", "y", "x", -1, -1, i, t, e, a, s, 0), f("z", "y", "x", 1, -1, i, t, -e, a, s, 1), f("x", "z", "y", 1, 1, e, i, t, n, a, 2), f("x", "z", "y", 1, -1, e, i, -t, n, a, 3), f("x", "y", "z", 1, -1, e, t, i, n, s, 4), f("x", "y", "z", -1, -1, e, t, -i, n, s, 5), this.setIndex(l), this.setAttribute("position", new Jt(c, 3)), this.setAttribute("normal", new Jt(h, 3)), this.setAttribute("uv", new Jt(d, 2));
    function f(v, m, g, E, R, y, S, T, A, x, b) {
      const N = y / A, O = S / x, k = y / 2, q = S / 2, B = T / 2, G = A + 1, Z = x + 1;
      let X = 0, te = 0;
      const V = new P();
      for (let K = 0; K < Z; K++) {
        const ee = K * O - q;
        for (let Ue = 0; Ue < G; Ue++) {
          const ue = Ue * N - k;
          V[v] = ue * E, V[m] = ee * R, V[g] = B, c.push(V.x, V.y, V.z), V[v] = 0, V[m] = 0, V[g] = T > 0 ? 1 : -1, h.push(V.x, V.y, V.z), d.push(Ue / A), d.push(1 - K / x), X += 1;
        }
      }
      for (let K = 0; K < x; K++) for (let ee = 0; ee < A; ee++) {
        const Ue = u + ee + G * K, ue = u + ee + G * (K + 1), Ye = u + (ee + 1) + G * (K + 1), ke = u + (ee + 1) + G * K;
        l.push(Ue, ue, ke), l.push(ue, Ye, ke), te += 6;
      }
      o.addGroup(p, te, b), p += te, u += X;
    }
  }
  copy(e) {
    return super.copy(e), this.parameters = Object.assign({}, e.parameters), this;
  }
  static fromJSON(e) {
    return new ps(e.width, e.height, e.depth, e.widthSegments, e.heightSegments, e.depthSegments);
  }
}
class Sa extends yt {
  constructor(e = 1, t = 1, i = 1, n = 1) {
    super(), this.type = "PlaneGeometry", this.parameters = { width: e, height: t, widthSegments: i, heightSegments: n };
    const s = e / 2, a = t / 2, o = Math.floor(i), l = Math.floor(n), c = o + 1, h = l + 1, d = e / o, u = t / l, p = [], f = [], v = [], m = [];
    for (let g = 0; g < h; g++) {
      const E = g * u - a;
      for (let R = 0; R < c; R++) {
        const y = R * d - s;
        f.push(y, -E, 0), v.push(0, 0, 1), m.push(R / o), m.push(1 - g / l);
      }
    }
    for (let g = 0; g < l; g++) for (let E = 0; E < o; E++) {
      const R = E + c * g, y = E + c * (g + 1), S = E + 1 + c * (g + 1), T = E + 1 + c * g;
      p.push(R, y, T), p.push(y, S, T);
    }
    this.setIndex(p), this.setAttribute("position", new Jt(f, 3)), this.setAttribute("normal", new Jt(v, 3)), this.setAttribute("uv", new Jt(m, 2));
  }
  copy(e) {
    return super.copy(e), this.parameters = Object.assign({}, e.parameters), this;
  }
  static fromJSON(e) {
    return new Sa(e.width, e.height, e.widthSegments, e.heightSegments);
  }
}
function xn(r) {
  const e = {};
  for (const t in r) {
    e[t] = {};
    for (const i in r[t]) {
      const n = r[t][i];
      if (Wc(n)) n.isRenderTargetTexture ? (Re("UniformsUtils: Textures of render targets cannot be cloned via cloneUniforms() or mergeUniforms()."), e[t][i] = null) : e[t][i] = n.clone();
      else if (Array.isArray(n)) if (Wc(n[0])) {
        const s = [];
        for (let a = 0, o = n.length; a < o; a++) s[a] = n[a].clone();
        e[t][i] = s;
      } else e[t][i] = n.slice();
      else e[t][i] = n;
    }
  }
  return e;
}
function kt(r) {
  const e = {};
  for (let t = 0; t < r.length; t++) {
    const i = xn(r[t]);
    for (const n in i) e[n] = i[n];
  }
  return e;
}
function Wc(r) {
  return r && (r.isColor || r.isMatrix3 || r.isMatrix4 || r.isVector2 || r.isVector3 || r.isVector4 || r.isTexture || r.isQuaternion);
}
function zf(r) {
  const e = [];
  for (let t = 0; t < r.length; t++) e.push(r[t].clone());
  return e;
}
function nd(r) {
  const e = r.getRenderTarget();
  return e === null ? r.outputColorSpace : e.isXRRenderTarget === !0 ? e.texture.colorSpace : Ze.workingColorSpace;
}
const sd = { clone: xn, merge: kt };
var Gf = `void main() {
	gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );
}`, Wf = `void main() {
	gl_FragColor = vec4( 1.0, 0.0, 0.0, 1.0 );
}`;
class xi extends vi {
  constructor(e) {
    super(), this.isShaderMaterial = !0, this.type = "ShaderMaterial", this.defines = {}, this.uniforms = {}, this.uniformsGroups = [], this.vertexShader = Gf, this.fragmentShader = Wf, this.linewidth = 1, this.wireframe = !1, this.wireframeLinewidth = 1, this.fog = !1, this.lights = !1, this.clipping = !1, this.forceSinglePass = !0, this.extensions = { clipCullDistance: !1, multiDraw: !1 }, this.defaultAttributeValues = { color: [1, 1, 1], uv: [0, 0], uv1: [0, 0] }, this.index0AttributeName = void 0, this.uniformsNeedUpdate = !1, this.glslVersion = null, e !== void 0 && this.setValues(e);
  }
  copy(e) {
    return super.copy(e), this.fragmentShader = e.fragmentShader, this.vertexShader = e.vertexShader, this.uniforms = xn(e.uniforms), this.uniformsGroups = zf(e.uniformsGroups), this.defines = Object.assign({}, e.defines), this.wireframe = e.wireframe, this.wireframeLinewidth = e.wireframeLinewidth, this.fog = e.fog, this.lights = e.lights, this.clipping = e.clipping, this.extensions = Object.assign({}, e.extensions), this.glslVersion = e.glslVersion, this.defaultAttributeValues = Object.assign({}, e.defaultAttributeValues), this.index0AttributeName = e.index0AttributeName, this.uniformsNeedUpdate = e.uniformsNeedUpdate, this;
  }
  toJSON(e) {
    const t = super.toJSON(e);
    t.glslVersion = this.glslVersion, t.uniforms = {};
    for (const n in this.uniforms) {
      const s = this.uniforms[n].value;
      s && s.isTexture ? t.uniforms[n] = { type: "t", value: s.toJSON(e).uuid } : s && s.isColor ? t.uniforms[n] = { type: "c", value: s.getHex() } : s && s.isVector2 ? t.uniforms[n] = { type: "v2", value: s.toArray() } : s && s.isVector3 ? t.uniforms[n] = { type: "v3", value: s.toArray() } : s && s.isVector4 ? t.uniforms[n] = { type: "v4", value: s.toArray() } : s && s.isMatrix3 ? t.uniforms[n] = { type: "m3", value: s.toArray() } : s && s.isMatrix4 ? t.uniforms[n] = { type: "m4", value: s.toArray() } : t.uniforms[n] = { value: s };
    }
    Object.keys(this.defines).length > 0 && (t.defines = this.defines), t.vertexShader = this.vertexShader, t.fragmentShader = this.fragmentShader, t.lights = this.lights, t.clipping = this.clipping;
    const i = {};
    for (const n in this.extensions) this.extensions[n] === !0 && (i[n] = !0);
    return Object.keys(i).length > 0 && (t.extensions = i), t;
  }
  fromJSON(e, t) {
    if (super.fromJSON(e, t), e.uniforms !== void 0) for (const i in e.uniforms) {
      const n = e.uniforms[i];
      switch (this.uniforms[i] = {}, n.type) {
        case "t":
          this.uniforms[i].value = t[n.value] || null;
          break;
        case "c":
          this.uniforms[i].value = new be().setHex(n.value);
          break;
        case "v2":
          this.uniforms[i].value = new We().fromArray(n.value);
          break;
        case "v3":
          this.uniforms[i].value = new P().fromArray(n.value);
          break;
        case "v4":
          this.uniforms[i].value = new gt().fromArray(n.value);
          break;
        case "m3":
          this.uniforms[i].value = new De().fromArray(n.value);
          break;
        case "m4":
          this.uniforms[i].value = new Fe().fromArray(n.value);
          break;
        default:
          this.uniforms[i].value = n.value;
      }
    }
    if (e.defines !== void 0 && (this.defines = e.defines), e.vertexShader !== void 0 && (this.vertexShader = e.vertexShader), e.fragmentShader !== void 0 && (this.fragmentShader = e.fragmentShader), e.glslVersion !== void 0 && (this.glslVersion = e.glslVersion), e.extensions !== void 0) for (const i in e.extensions) this.extensions[i] = e.extensions[i];
    return e.lights !== void 0 && (this.lights = e.lights), e.clipping !== void 0 && (this.clipping = e.clipping), this;
  }
}
class Xf extends xi {
  constructor(e) {
    super(e), this.isRawShaderMaterial = !0, this.type = "RawShaderMaterial";
  }
}
class Zl extends vi {
  constructor(e) {
    super(), this.isMeshStandardMaterial = !0, this.type = "MeshStandardMaterial", this.defines = { STANDARD: "" }, this.color = new be(16777215), this.roughness = 1, this.metalness = 0, this.map = null, this.lightMap = null, this.lightMapIntensity = 1, this.aoMap = null, this.aoMapIntensity = 1, this.emissive = new be(0), this.emissiveIntensity = 1, this.emissiveMap = null, this.bumpMap = null, this.bumpScale = 1, this.normalMap = null, this.normalMapType = la, this.normalScale = new We(1, 1), this.displacementMap = null, this.displacementScale = 1, this.displacementBias = 0, this.roughnessMap = null, this.metalnessMap = null, this.alphaMap = null, this.envMap = null, this.envMapRotation = new Yt(), this.envMapIntensity = 1, this.wireframe = !1, this.wireframeLinewidth = 1, this.wireframeLinecap = "round", this.wireframeLinejoin = "round", this.flatShading = !1, this.fog = !0, this.setValues(e);
  }
  copy(e) {
    return super.copy(e), this.defines = { STANDARD: "" }, this.color.copy(e.color), this.roughness = e.roughness, this.metalness = e.metalness, this.map = e.map, this.lightMap = e.lightMap, this.lightMapIntensity = e.lightMapIntensity, this.aoMap = e.aoMap, this.aoMapIntensity = e.aoMapIntensity, this.emissive.copy(e.emissive), this.emissiveMap = e.emissiveMap, this.emissiveIntensity = e.emissiveIntensity, this.bumpMap = e.bumpMap, this.bumpScale = e.bumpScale, this.normalMap = e.normalMap, this.normalMapType = e.normalMapType, this.normalScale.copy(e.normalScale), this.displacementMap = e.displacementMap, this.displacementScale = e.displacementScale, this.displacementBias = e.displacementBias, this.roughnessMap = e.roughnessMap, this.metalnessMap = e.metalnessMap, this.alphaMap = e.alphaMap, this.envMap = e.envMap, this.envMapRotation.copy(e.envMapRotation), this.envMapIntensity = e.envMapIntensity, this.wireframe = e.wireframe, this.wireframeLinewidth = e.wireframeLinewidth, this.wireframeLinecap = e.wireframeLinecap, this.wireframeLinejoin = e.wireframeLinejoin, this.flatShading = e.flatShading, this.fog = e.fog, this;
  }
}
class Oi extends Zl {
  constructor(e) {
    super(), this.isMeshPhysicalMaterial = !0, this.defines = { STANDARD: "", PHYSICAL: "" }, this.type = "MeshPhysicalMaterial", this.anisotropyRotation = 0, this.anisotropyMap = null, this.clearcoatMap = null, this.clearcoatRoughness = 0, this.clearcoatRoughnessMap = null, this.clearcoatNormalScale = new We(1, 1), this.clearcoatNormalMap = null, this.ior = 1.5, Object.defineProperty(this, "reflectivity", { get: function() {
      return $e(2.5 * (this.ior - 1) / (this.ior + 1), 0, 1);
    }, set: function(t) {
      this.ior = (1 + 0.4 * t) / (1 - 0.4 * t);
    } }), this.iridescenceMap = null, this.iridescenceIOR = 1.3, this.iridescenceThicknessRange = [100, 400], this.iridescenceThicknessMap = null, this.sheenColor = new be(0), this.sheenColorMap = null, this.sheenRoughness = 1, this.sheenRoughnessMap = null, this.transmissionMap = null, this.thickness = 0, this.thicknessMap = null, this.attenuationDistance = 1 / 0, this.attenuationColor = new be(1, 1, 1), this.specularIntensity = 1, this.specularIntensityMap = null, this.specularColor = new be(1, 1, 1), this.specularColorMap = null, this._anisotropy = 0, this._clearcoat = 0, this._dispersion = 0, this._iridescence = 0, this._retroreflectivity = 0, this._sheen = 0, this._transmission = 0, this.setValues(e);
  }
  get anisotropy() {
    return this._anisotropy;
  }
  set anisotropy(e) {
    this._anisotropy > 0 != e > 0 && this.version++, this._anisotropy = e;
  }
  get clearcoat() {
    return this._clearcoat;
  }
  set clearcoat(e) {
    this._clearcoat > 0 != e > 0 && this.version++, this._clearcoat = e;
  }
  get iridescence() {
    return this._iridescence;
  }
  set iridescence(e) {
    this._iridescence > 0 != e > 0 && this.version++, this._iridescence = e;
  }
  get dispersion() {
    return this._dispersion;
  }
  set dispersion(e) {
    this._dispersion > 0 != e > 0 && this.version++, this._dispersion = e;
  }
  get retroreflectivity() {
    return this._retroreflectivity;
  }
  set retroreflectivity(e) {
    this._retroreflectivity > 0 != e > 0 && this.version++, this._retroreflectivity = e;
  }
  get sheen() {
    return this._sheen;
  }
  set sheen(e) {
    this._sheen > 0 != e > 0 && this.version++, this._sheen = e;
  }
  get transmission() {
    return this._transmission;
  }
  set transmission(e) {
    this._transmission > 0 != e > 0 && this.version++, this._transmission = e;
  }
  copy(e) {
    return super.copy(e), this.defines = { STANDARD: "", PHYSICAL: "" }, this.anisotropy = e.anisotropy, this.anisotropyRotation = e.anisotropyRotation, this.anisotropyMap = e.anisotropyMap, this.clearcoat = e.clearcoat, this.clearcoatMap = e.clearcoatMap, this.clearcoatRoughness = e.clearcoatRoughness, this.clearcoatRoughnessMap = e.clearcoatRoughnessMap, this.clearcoatNormalMap = e.clearcoatNormalMap, this.clearcoatNormalScale.copy(e.clearcoatNormalScale), this.dispersion = e.dispersion, this.ior = e.ior, this.iridescence = e.iridescence, this.iridescenceMap = e.iridescenceMap, this.iridescenceIOR = e.iridescenceIOR, this.iridescenceThicknessRange = [...e.iridescenceThicknessRange], this.iridescenceThicknessMap = e.iridescenceThicknessMap, this.retroreflectivity = e.retroreflectivity, this.sheen = e.sheen, this.sheenColor.copy(e.sheenColor), this.sheenColorMap = e.sheenColorMap, this.sheenRoughness = e.sheenRoughness, this.sheenRoughnessMap = e.sheenRoughnessMap, this.transmission = e.transmission, this.transmissionMap = e.transmissionMap, this.thickness = e.thickness, this.thicknessMap = e.thicknessMap, this.attenuationDistance = e.attenuationDistance, this.attenuationColor.copy(e.attenuationColor), this.specularIntensity = e.specularIntensity, this.specularIntensityMap = e.specularIntensityMap, this.specularColor.copy(e.specularColor), this.specularColorMap = e.specularColorMap, this;
  }
}
class jf extends vi {
  constructor(e) {
    super(), this.isMeshDepthMaterial = !0, this.type = "MeshDepthMaterial", this.depthPacking = Dp, this.map = null, this.alphaMap = null, this.displacementMap = null, this.displacementScale = 1, this.displacementBias = 0, this.wireframe = !1, this.wireframeLinewidth = 1, this.setValues(e);
  }
  copy(e) {
    return super.copy(e), this.depthPacking = e.depthPacking, this.map = e.map, this.alphaMap = e.alphaMap, this.displacementMap = e.displacementMap, this.displacementScale = e.displacementScale, this.displacementBias = e.displacementBias, this.wireframe = e.wireframe, this.wireframeLinewidth = e.wireframeLinewidth, this;
  }
}
class qf extends vi {
  constructor(e) {
    super(), this.isMeshDistanceMaterial = !0, this.type = "MeshDistanceMaterial", this.map = null, this.alphaMap = null, this.displacementMap = null, this.displacementScale = 1, this.displacementBias = 0, this.setValues(e);
  }
  copy(e) {
    return super.copy(e), this.map = e.map, this.alphaMap = e.alphaMap, this.displacementMap = e.displacementMap, this.displacementScale = e.displacementScale, this.displacementBias = e.displacementBias, this;
  }
}
function dr(r, e) {
  return !r || r.constructor === e ? r : typeof e.BYTES_PER_ELEMENT == "number" ? new e(r) : Array.prototype.slice.call(r);
}
function Js(r) {
  return r !== void 0 && r.inTangents !== void 0 && r.outTangents !== void 0;
}
function Yf(r) {
  function e(n, s) {
    return r[n] - r[s];
  }
  const t = r.length, i = new Array(t);
  for (let n = 0; n !== t; ++n) i[n] = n;
  return i.sort(e), i;
}
function Xc(r, e, t) {
  const i = r.length, n = new r.constructor(i);
  for (let s = 0, a = 0; a !== i; ++s) {
    const o = t[s] * e;
    for (let l = 0; l !== e; ++l) n[a++] = r[o + l];
  }
  return n;
}
function Kf(r, e, t, i) {
  let n = 1, s = r[0];
  for (; s !== void 0 && s[i] === void 0; ) s = r[n++];
  if (s === void 0) return;
  let a = s[i];
  if (a !== void 0) if (Array.isArray(a)) do
    a = s[i], a !== void 0 && (e.push(s.time), t.push(...a)), s = r[n++];
  while (s !== void 0);
  else if (a.toArray !== void 0) do
    a = s[i], a !== void 0 && (e.push(s.time), a.toArray(t, t.length)), s = r[n++];
  while (s !== void 0);
  else do
    a = s[i], a !== void 0 && (e.push(s.time), t.push(a)), s = r[n++];
  while (s !== void 0);
}
class Tn {
  constructor(e, t, i, n) {
    this.parameterPositions = e, this._cachedIndex = 0, this.resultBuffer = n !== void 0 ? n : new t.constructor(i), this.sampleValues = t, this.valueSize = i, this.settings = null, this.DefaultSettings_ = {};
  }
  evaluate(e) {
    const t = this.parameterPositions;
    let i = this._cachedIndex, n = t[i], s = t[i - 1];
    e: {
      t: {
        let a;
        i: {
          r: if (!(e < n)) {
            for (let o = i + 2; ; ) {
              if (n === void 0) {
                if (e < s) break r;
                return i = t.length, this._cachedIndex = i, this.copySampleValue_(i - 1);
              }
              if (i === o) break;
              if (s = n, n = t[++i], e < n) break t;
            }
            a = t.length;
            break i;
          }
          if (!(e >= s)) {
            const o = t[1];
            e < o && (i = 2, s = o);
            for (let l = i - 2; ; ) {
              if (s === void 0) return this._cachedIndex = 0, this.copySampleValue_(0);
              if (i === l) break;
              if (n = s, s = t[--i - 1], e >= s) break t;
            }
            a = i, i = 0;
            break i;
          }
          break e;
        }
        for (; i < a; ) {
          const o = i + a >>> 1;
          e < t[o] ? a = o : i = o + 1;
        }
        if (n = t[i], s = t[i - 1], s === void 0) return this._cachedIndex = 0, this.copySampleValue_(0);
        if (n === void 0) return i = t.length, this._cachedIndex = i, this.copySampleValue_(i - 1);
      }
      this._cachedIndex = i, this.intervalChanged_(i, s, n);
    }
    return this.interpolate_(i, s, e, n);
  }
  getSettings_() {
    return this.settings || this.DefaultSettings_;
  }
  copySampleValue_(e) {
    const t = this.resultBuffer, i = this.sampleValues, n = this.valueSize, s = e * n;
    for (let a = 0; a !== n; ++a) t[a] = i[s + a];
    return t;
  }
  interpolate_() {
    throw new Error("THREE.Interpolant: Call to abstract method.");
  }
  intervalChanged_() {
  }
}
class Zf extends Tn {
  constructor(e, t, i, n) {
    super(e, t, i, n), this._weightPrev = -0, this._offsetPrev = -0, this._weightNext = -0, this._offsetNext = -0, this.DefaultSettings_ = { endingStart: hn, endingEnd: hn };
  }
  intervalChanged_(e, t, i) {
    const n = this.parameterPositions;
    let s = e - 2, a = e + 1, o = n[s], l = n[a];
    if (o === void 0) switch (this.getSettings_().endingStart) {
      case un:
        s = e, o = 2 * t - i;
        break;
      case oa:
        s = n.length - 2, o = t + n[s] - n[s + 1];
        break;
      default:
        s = e, o = i;
    }
    if (l === void 0) switch (this.getSettings_().endingEnd) {
      case un:
        a = e, l = 2 * i - t;
        break;
      case oa:
        a = 1, l = i + n[1] - n[0];
        break;
      default:
        a = e - 1, l = t;
    }
    const c = (i - t) * 0.5, h = this.valueSize;
    this._weightPrev = c / (t - o), this._weightNext = c / (l - i), this._offsetPrev = s * h, this._offsetNext = a * h;
  }
  interpolate_(e, t, i, n) {
    const s = this.resultBuffer, a = this.sampleValues, o = this.valueSize, l = e * o, c = l - o, h = this._offsetPrev, d = this._offsetNext, u = this._weightPrev, p = this._weightNext, f = (i - t) / (n - t), v = f * f, m = v * f, g = -u * m + 2 * u * v - u * f, E = (1 + u) * m + (-1.5 - 2 * u) * v + (-0.5 + u) * f + 1, R = (-1 - p) * m + (1.5 + p) * v + 0.5 * f, y = p * m - p * v;
    for (let S = 0; S !== o; ++S) s[S] = g * a[h + S] + E * a[c + S] + R * a[l + S] + y * a[d + S];
    return s;
  }
}
class ad extends Tn {
  constructor(e, t, i, n) {
    super(e, t, i, n);
  }
  interpolate_(e, t, i, n) {
    const s = this.resultBuffer, a = this.sampleValues, o = this.valueSize, l = e * o, c = l - o, h = (i - t) / (n - t), d = 1 - h;
    for (let u = 0; u !== o; ++u) s[u] = a[c + u] * d + a[l + u] * h;
    return s;
  }
}
class $f extends Tn {
  constructor(e, t, i, n) {
    super(e, t, i, n);
  }
  interpolate_(e) {
    return this.copySampleValue_(e - 1);
  }
}
class Qf extends Tn {
  interpolate_(e, t, i, n) {
    const s = this.resultBuffer, a = this.sampleValues, o = this.valueSize, l = e * o, c = l - o, h = this.inTangents, d = this.outTangents;
    if (!h || !d) {
      const f = (i - t) / (n - t), v = 1 - f;
      for (let m = 0; m !== o; ++m) s[m] = a[c + m] * v + a[l + m] * f;
      return s;
    }
    const u = o * 2, p = e - 1;
    for (let f = 0; f !== o; ++f) {
      const v = a[c + f], m = a[l + f], g = p * u + f * 2, E = d[g], R = d[g + 1], y = e * u + f * 2, S = h[y], T = h[y + 1], A = em(i, t, E, S, n);
      s[f] = od(A, v, R, T, m);
    }
    return s;
  }
}
function od(r, e, t, i, n) {
  const s = 1 - r;
  return s * s * s * e + 3 * s * s * r * t + 3 * s * r * r * i + r * r * r * n;
}
function Jf(r, e, t, i, n) {
  const s = 1 - r;
  return 3 * s * s * (t - e) + 6 * s * r * (i - t) + 3 * r * r * (n - i);
}
function em(r, e, t, i, n) {
  let s = (r - e) / (n - e);
  for (let a = 0; a < 8; a++) {
    const o = od(s, e, t, i, n) - r;
    if (Math.abs(o) < 1e-10) break;
    const l = Jf(s, e, t, i, n);
    if (Math.abs(l) < 1e-10) break;
    s = Math.max(0, Math.min(1, s - o / l));
  }
  return s;
}
class mi {
  constructor(e, t, i, n) {
    if (e === void 0) throw new Error("THREE.KeyframeTrack: track name is undefined");
    if (t === void 0 || t.length === 0) throw new Error("THREE.KeyframeTrack: no keyframes in track named " + e);
    this.name = e, this.times = dr(t, this.TimeBufferType), this.values = dr(i, this.ValueBufferType), this.setInterpolation(n || this.DefaultInterpolation);
  }
  static toJSON(e) {
    const t = e.constructor;
    let i;
    if (t.toJSON !== this.toJSON) i = t.toJSON(e);
    else {
      i = { name: e.name, times: dr(e.times, Array), values: dr(e.values, Array) };
      const n = e.getInterpolation();
      n !== e.DefaultInterpolation && (i.interpolation = n), Js(e.settings) && (i.settings = { inTangents: dr(e.settings.inTangents, Array), outTangents: dr(e.settings.outTangents, Array) });
    }
    return i.type = e.ValueTypeName, i;
  }
  InterpolantFactoryMethodDiscrete(e) {
    return new $f(this.times, this.values, this.getValueSize(), e);
  }
  InterpolantFactoryMethodLinear(e) {
    return new ad(this.times, this.values, this.getValueSize(), e);
  }
  InterpolantFactoryMethodSmooth(e) {
    return new Zf(this.times, this.values, this.getValueSize(), e);
  }
  InterpolantFactoryMethodBezier(e) {
    const t = new Qf(this.times, this.values, this.getValueSize(), e);
    return this.settings && (t.inTangents = this.settings.inTangents, t.outTangents = this.settings.outTangents), t;
  }
  setInterpolation(e) {
    let t;
    switch (e) {
      case ss:
        t = this.InterpolantFactoryMethodDiscrete;
        break;
      case as:
        t = this.InterpolantFactoryMethodLinear;
        break;
      case Na:
        t = this.InterpolantFactoryMethodSmooth;
        break;
      case pc:
        t = this.InterpolantFactoryMethodBezier;
        break;
    }
    if (t === void 0) {
      const i = "unsupported interpolation for " + this.ValueTypeName + " keyframe track named " + this.name;
      if (this.createInterpolant === void 0) if (e !== this.DefaultInterpolation) this.setInterpolation(this.DefaultInterpolation);
      else throw new Error(i);
      return Re("KeyframeTrack:", i), this;
    }
    return this.createInterpolant = t, this;
  }
  getInterpolation() {
    switch (this.createInterpolant) {
      case this.InterpolantFactoryMethodDiscrete:
        return ss;
      case this.InterpolantFactoryMethodLinear:
        return as;
      case this.InterpolantFactoryMethodSmooth:
        return Na;
      case this.InterpolantFactoryMethodBezier:
        return pc;
    }
  }
  getValueSize() {
    return this.values.length / this.times.length;
  }
  shift(e) {
    if (e !== 0) {
      const t = this.times;
      for (let i = 0, n = t.length; i !== n; ++i) t[i] += e;
    }
    return this;
  }
  scale(e) {
    if (e !== 1) {
      const t = this.times;
      for (let i = 0, n = t.length; i !== n; ++i) t[i] *= e;
      Js(this.settings) && (jc(this.settings.inTangents, e), jc(this.settings.outTangents, e));
    }
    return this;
  }
  trim(e, t) {
    const i = this.times, n = i.length;
    let s = 0, a = n - 1;
    for (; s !== n && i[s] < e; ) ++s;
    for (; a !== -1 && i[a] > t; ) --a;
    if (++a, s !== 0 || a !== n) {
      s >= a && (a = Math.max(a, 1), s = a - 1);
      const o = this.getValueSize();
      this.times = i.slice(s, a), this.values = this.values.slice(s * o, a * o);
    }
    return this;
  }
  validate() {
    let e = !0;
    const t = this.getValueSize();
    t - Math.floor(t) !== 0 && (Be("KeyframeTrack: Invalid value size in track.", this), e = !1);
    const i = this.times, n = this.values, s = i.length;
    s === 0 && (Be("KeyframeTrack: Track is empty.", this), e = !1);
    let a = null;
    for (let o = 0; o !== s; o++) {
      const l = i[o];
      if (typeof l == "number" && isNaN(l)) {
        Be("KeyframeTrack: Time is not a valid number.", this, o, l), e = !1;
        break;
      }
      if (a !== null && a > l) {
        Be("KeyframeTrack: Out of order keys.", this, o, l, a), e = !1;
        break;
      }
      a = l;
    }
    if (n !== void 0 && jp(n)) for (let o = 0, l = n.length; o !== l; ++o) {
      const c = n[o];
      if (isNaN(c)) {
        Be("KeyframeTrack: Value is not a valid number.", this, o, c), e = !1;
        break;
      }
    }
    return e;
  }
  optimize() {
    const e = this.times.slice(), t = this.values.slice(), i = this.getValueSize(), n = this.getInterpolation() === Na, s = e.length - 1;
    let a = 1;
    for (let o = 1; o < s; ++o) {
      let l = !1;
      const c = e[o], h = e[o + 1];
      if (c !== h && (o !== 1 || c !== e[0])) if (n) l = !0;
      else {
        const d = o * i, u = d - i, p = d + i;
        for (let f = 0; f !== i; ++f) {
          const v = t[d + f];
          if (v !== t[u + f] || v !== t[p + f]) {
            l = !0;
            break;
          }
        }
      }
      if (l) {
        if (o !== a) {
          e[a] = e[o];
          const d = o * i, u = a * i;
          for (let p = 0; p !== i; ++p) t[u + p] = t[d + p];
        }
        ++a;
      }
    }
    if (s > 0) {
      e[a] = e[s];
      for (let o = s * i, l = a * i, c = 0; c !== i; ++c) t[l + c] = t[o + c];
      ++a;
    }
    return a !== e.length ? (this.times = e.slice(0, a), this.values = t.slice(0, a * i)) : (this.times = e, this.values = t), this;
  }
  clone() {
    const e = this.times.slice(), t = this.values.slice(), i = this.constructor, n = new i(this.name, e, t);
    return n.createInterpolant = this.createInterpolant, Js(this.settings) && (n.settings = { inTangents: this.settings.inTangents.slice(), outTangents: this.settings.outTangents.slice() }), n;
  }
}
function jc(r, e) {
  for (let t = 0, i = r.length; t !== i; t += 2) r[t] *= e;
}
mi.prototype.ValueTypeName = "", mi.prototype.TimeBufferType = Float32Array, mi.prototype.ValueBufferType = Float32Array, mi.prototype.DefaultInterpolation = as;
class an extends mi {
  constructor(e, t, i) {
    super(e, t, i);
  }
}
an.prototype.ValueTypeName = "bool", an.prototype.ValueBufferType = Array, an.prototype.DefaultInterpolation = ss, an.prototype.InterpolantFactoryMethodLinear = void 0, an.prototype.InterpolantFactoryMethodSmooth = void 0;
class ld extends mi {
  constructor(e, t, i, n) {
    super(e, t, i, n);
  }
}
ld.prototype.ValueTypeName = "color";
class Mn extends mi {
  constructor(e, t, i, n) {
    super(e, t, i, n);
  }
}
Mn.prototype.ValueTypeName = "number";
class tm extends Tn {
  constructor(e, t, i, n) {
    super(e, t, i, n);
  }
  interpolate_(e, t, i, n) {
    const s = this.resultBuffer, a = this.sampleValues, o = this.valueSize, l = (i - t) / (n - t);
    let c = e * o;
    for (let h = c + o; c !== h; c += 4) Ce.slerpFlat(s, 0, a, c - o, a, c, l);
    return s;
  }
}
class yn extends mi {
  constructor(e, t, i, n) {
    super(e, t, i, n);
  }
  InterpolantFactoryMethodLinear(e) {
    return new tm(this.times, this.values, this.getValueSize(), e);
  }
}
yn.prototype.ValueTypeName = "quaternion", yn.prototype.InterpolantFactoryMethodSmooth = void 0;
class on extends mi {
  constructor(e, t, i) {
    super(e, t, i);
  }
}
on.prototype.ValueTypeName = "string", on.prototype.ValueBufferType = Array, on.prototype.DefaultInterpolation = ss, on.prototype.InterpolantFactoryMethodLinear = void 0, on.prototype.InterpolantFactoryMethodSmooth = void 0;
class fa extends mi {
  constructor(e, t, i, n) {
    super(e, t, i, n);
  }
}
fa.prototype.ValueTypeName = "vector";
class hs {
  constructor(e = "", t = -1, i = [], n = zl) {
    this.name = e, this.tracks = i, this.duration = t, this.blendMode = n, this.uuid = gi(), this.userData = {}, this.duration < 0 && this.resetDuration();
  }
  static parse(e) {
    const t = [], i = e.tracks, n = 1 / (e.fps || 1);
    for (let a = 0, o = i.length; a !== o; ++a) t.push(rm(i[a]).scale(n));
    const s = new this(e.name, e.duration, t, e.blendMode);
    return s.uuid = e.uuid, s.userData = JSON.parse(e.userData || "{}"), s;
  }
  static toJSON(e) {
    const t = [], i = e.tracks, n = { name: e.name, duration: e.duration, tracks: t, uuid: e.uuid, blendMode: e.blendMode, userData: JSON.stringify(e.userData) };
    for (let s = 0, a = i.length; s !== a; ++s) t.push(mi.toJSON(i[s]));
    return n;
  }
  static CreateFromMorphTargetSequence(e, t, i, n) {
    const s = t.length, a = [];
    for (let o = 0; o < s; o++) {
      let l = [], c = [];
      l.push((o + s - 1) % s, o, (o + 1) % s), c.push(0, 1, 0);
      const h = Yf(l);
      l = Xc(l, 1, h), c = Xc(c, 1, h), !n && l[0] === 0 && (l.push(s), c.push(c[0])), a.push(new Mn(".morphTargetInfluences[" + t[o].name + "]", l, c).scale(1 / i));
    }
    return new this(e, -1, a);
  }
  static findByName(e, t) {
    let i = e;
    if (!Array.isArray(e)) {
      const n = e;
      i = n.geometry && n.geometry.animations || n.animations;
    }
    for (let n = 0; n < i.length; n++) if (i[n].name === t) return i[n];
    return null;
  }
  static CreateClipsFromMorphTargetSequences(e, t, i) {
    const n = {}, s = /^([\w-]*?)([\d]+)$/;
    for (let o = 0, l = e.length; o < l; o++) {
      const c = e[o], h = c.name.match(s);
      if (h && h.length > 1) {
        const d = h[1];
        let u = n[d];
        u || (n[d] = u = []), u.push(c);
      }
    }
    const a = [];
    for (const o in n) a.push(this.CreateFromMorphTargetSequence(o, n[o], t, i));
    return a;
  }
  resetDuration() {
    const e = this.tracks;
    let t = 0;
    for (let i = 0, n = e.length; i !== n; ++i) {
      const s = this.tracks[i];
      t = Math.max(t, s.times[s.times.length - 1]);
    }
    return this.duration = t, this;
  }
  trim() {
    for (let e = 0; e < this.tracks.length; e++) this.tracks[e].trim(0, this.duration);
    return this;
  }
  validate() {
    let e = !0;
    for (let t = 0; t < this.tracks.length; t++) e = e && this.tracks[t].validate();
    return e;
  }
  optimize() {
    for (let e = 0; e < this.tracks.length; e++) this.tracks[e].optimize();
    return this;
  }
  clone() {
    const e = [];
    for (let i = 0; i < this.tracks.length; i++) e.push(this.tracks[i].clone());
    const t = new this.constructor(this.name, this.duration, e, this.blendMode);
    return t.userData = JSON.parse(JSON.stringify(this.userData)), t;
  }
  toJSON() {
    return this.constructor.toJSON(this);
  }
}
function im(r) {
  switch (r.toLowerCase()) {
    case "scalar":
    case "double":
    case "float":
    case "number":
    case "integer":
      return Mn;
    case "vector":
    case "vector2":
    case "vector3":
    case "vector4":
      return fa;
    case "color":
      return ld;
    case "quaternion":
      return yn;
    case "bool":
    case "boolean":
      return an;
    case "string":
      return on;
  }
  throw new Error("THREE.KeyframeTrack: Unsupported typeName: " + r);
}
function rm(r) {
  if (r.type === void 0) throw new Error("THREE.KeyframeTrack: track type undefined, can not parse");
  const e = im(r.type);
  if (r.times === void 0) {
    const i = [], n = [];
    Kf(r.keys, i, n, "value"), r.times = i, r.values = n;
  }
  let t;
  return e.parse !== void 0 ? t = e.parse(r) : t = new e(r.name, r.times, r.values, r.interpolation), Js(r.settings) && (t.settings = { inTangents: dr(r.settings.inTangents, Float32Array), outTangents: dr(r.settings.outTangents, Float32Array) }), t;
}
const Zi = { enabled: !1, files: {}, add: function(r, e) {
  this.enabled !== !1 && (qc(r) || (this.files[r] = e));
}, get: function(r) {
  if (this.enabled !== !1 && !qc(r)) return this.files[r];
}, remove: function(r) {
  delete this.files[r];
}, clear: function() {
  this.files = {};
} };
function qc(r) {
  try {
    const e = r.slice(r.indexOf(":") + 1);
    return new URL(e).protocol === "blob:";
  } catch {
    return !1;
  }
}
class cd {
  constructor(e, t, i) {
    const n = this;
    let s = !1, a = 0, o = 0, l;
    const c = [];
    this.onStart = void 0, this.onLoad = e, this.onProgress = t, this.onError = i, this._abortController = null, this.itemStart = function(h) {
      o++, s === !1 && n.onStart !== void 0 && n.onStart(h, a, o), s = !0;
    }, this.itemEnd = function(h) {
      a++, n.onProgress !== void 0 && n.onProgress(h, a, o), a === o && (s = !1, n.onLoad !== void 0 && n.onLoad());
    }, this.itemError = function(h) {
      n.onError !== void 0 && n.onError(h);
    }, this.resolveURL = function(h) {
      return h = h.normalize("NFC"), l ? l(h) : h;
    }, this.setURLModifier = function(h) {
      return l = h, this;
    }, this.addHandler = function(h, d) {
      return c.push(h, d), this;
    }, this.removeHandler = function(h) {
      const d = c.indexOf(h);
      return d !== -1 && c.splice(d, 2), this;
    }, this.getHandler = function(h) {
      for (let d = 0, u = c.length; d < u; d += 2) {
        const p = c[d], f = c[d + 1];
        if (p.global && (p.lastIndex = 0), p.test(h)) return f;
      }
      return null;
    }, this.abort = function() {
      return this.abortController.abort(), this._abortController = null, this;
    };
  }
  get abortController() {
    return this._abortController || (this._abortController = new AbortController()), this._abortController;
  }
}
const nm = new cd();
class En {
  constructor(e) {
    this.manager = e !== void 0 ? e : nm, this.crossOrigin = "anonymous", this.withCredentials = !1, this.path = "", this.resourcePath = "", this.requestHeader = {}, typeof __THREE_DEVTOOLS__ < "u" && __THREE_DEVTOOLS__.dispatchEvent(new CustomEvent("observe", { detail: this }));
  }
  load() {
  }
  loadAsync(e, t) {
    const i = this;
    return new Promise(function(n, s) {
      i.load(e, n, t, s);
    });
  }
  parse() {
  }
  setCrossOrigin(e) {
    return this.crossOrigin = e, this;
  }
  setWithCredentials(e) {
    return this.withCredentials = e, this;
  }
  setPath(e) {
    return this.path = e, this;
  }
  setResourcePath(e) {
    return this.resourcePath = e, this;
  }
  setRequestHeader(e) {
    return this.requestHeader = e, this;
  }
  abort() {
    return this;
  }
}
En.DEFAULT_MATERIAL_NAME = "__DEFAULT";
const ji = {};
class sm extends Error {
  constructor(e, t) {
    super(e), this.response = t;
  }
}
class hd extends En {
  constructor(e) {
    super(e), this.mimeType = "", this.responseType = "", this._abortController = new AbortController();
  }
  load(e, t, i, n) {
    e === void 0 && (e = ""), this.path !== void 0 && (e = this.path + e), e = this.manager.resolveURL(e);
    const s = Zi.get(`file:${e}`);
    if (s !== void 0) {
      this.manager.itemStart(e), setTimeout(() => {
        t && t(s), this.manager.itemEnd(e);
      }, 0);
      return;
    }
    if (ji[e] !== void 0) {
      ji[e].push({ onLoad: t, onProgress: i, onError: n });
      return;
    }
    ji[e] = [], ji[e].push({ onLoad: t, onProgress: i, onError: n });
    const a = new Request(e, { headers: new Headers(this.requestHeader), credentials: this.withCredentials ? "include" : "same-origin", signal: typeof AbortSignal.any == "function" ? AbortSignal.any([this._abortController.signal, this.manager.abortController.signal]) : this._abortController.signal }), o = this.mimeType, l = this.responseType;
    fetch(a).then((c) => {
      if (c.status === 200 || c.status === 0) {
        if (c.status === 0 && Re("FileLoader: HTTP Status 0 received."), typeof ReadableStream > "u" || c.body === void 0 || c.body.getReader === void 0) return c;
        const h = ji[e], d = c.body.getReader(), u = c.headers.get("X-File-Size") || c.headers.get("Content-Length"), p = u ? parseInt(u) : 0, f = p !== 0;
        let v = 0;
        const m = new ReadableStream({ start(g) {
          E();
          function E() {
            d.read().then(({ done: R, value: y }) => {
              if (R) g.close();
              else {
                v += y.byteLength;
                const S = new ProgressEvent("progress", { lengthComputable: f, loaded: v, total: p });
                for (let T = 0, A = h.length; T < A; T++) {
                  const x = h[T];
                  x.onProgress && x.onProgress(S);
                }
                g.enqueue(y), E();
              }
            }, (R) => {
              g.error(R);
            });
          }
        } });
        return new Response(m);
      } else throw new sm(`fetch for "${c.url}" responded with ${c.status}: ${c.statusText}`, c);
    }).then((c) => {
      switch (l) {
        case "arraybuffer":
          return c.arrayBuffer();
        case "blob":
          return c.blob();
        case "document":
          return c.text().then((h) => new DOMParser().parseFromString(h, o));
        case "json":
          return c.json();
        default:
          if (o === "") return c.text();
          {
            const h = /charset="?([^;"\s]*)"?/i.exec(o), d = h && h[1] ? h[1].toLowerCase() : void 0, u = new TextDecoder(d);
            return c.arrayBuffer().then((p) => u.decode(p));
          }
      }
    }).then((c) => {
      Zi.add(`file:${e}`, c);
      const h = ji[e];
      delete ji[e];
      for (let d = 0, u = h.length; d < u; d++) {
        const p = h[d];
        p.onLoad && p.onLoad(c);
      }
    }).catch((c) => {
      const h = ji[e];
      if (h === void 0) throw this.manager.itemError(e), c;
      delete ji[e];
      for (let d = 0, u = h.length; d < u; d++) {
        const p = h[d];
        p.onError && p.onError(c);
      }
      this.manager.itemError(e);
    }).finally(() => {
      this.manager.itemEnd(e);
    }), this.manager.itemStart(e);
  }
  setResponseType(e) {
    return this.responseType = e, this;
  }
  setMimeType(e) {
    return this.mimeType = e, this;
  }
  abort() {
    return this._abortController.abort(), this._abortController = new AbortController(), this;
  }
}
const Kr = /* @__PURE__ */ new WeakMap();
class ud extends En {
  constructor(e) {
    super(e);
  }
  load(e, t, i, n) {
    this.path !== void 0 && (e = this.path + e), e = this.manager.resolveURL(e);
    const s = this, a = Zi.get(`image:${e}`);
    if (a !== void 0) {
      if (a.complete === !0) s.manager.itemStart(e), setTimeout(function() {
        t && t(a), s.manager.itemEnd(e);
      }, 0);
      else {
        let d = Kr.get(a);
        d === void 0 && (d = [], Kr.set(a, d)), d.push({ onLoad: t, onError: n });
      }
      return a;
    }
    const o = ls("img");
    function l() {
      h(), t && t(this);
      const d = Kr.get(this) || [];
      for (let u = 0; u < d.length; u++) {
        const p = d[u];
        p.onLoad && p.onLoad(this);
      }
      Kr.delete(this), s.manager.itemEnd(e);
    }
    function c(d) {
      h(), n && n(d), Zi.remove(`image:${e}`);
      const u = Kr.get(this) || [];
      for (let p = 0; p < u.length; p++) {
        const f = u[p];
        f.onError && f.onError(d);
      }
      Kr.delete(this), s.manager.itemError(e), s.manager.itemEnd(e);
    }
    function h() {
      o.removeEventListener("load", l, !1), o.removeEventListener("error", c, !1);
    }
    return o.addEventListener("load", l, !1), o.addEventListener("error", c, !1), e.slice(0, 5) !== "data:" && this.crossOrigin !== void 0 && (o.crossOrigin = this.crossOrigin), Zi.add(`image:${e}`, o), s.manager.itemStart(e), o.src = e, o;
  }
}
class am extends En {
  constructor(e) {
    super(e);
  }
  load(e, t, i, n) {
    const s = new Nt(), a = new ud(this.manager);
    return a.setCrossOrigin(this.crossOrigin), a.setPath(this.path), a.load(e, function(o) {
      s.image = o, s.needsUpdate = !0, t !== void 0 && t(s);
    }, i, n), s;
  }
}
class Ta extends at {
  constructor(e, t = 1) {
    super(), this.isLight = !0, this.type = "Light", this.color = new be(e), this.intensity = t;
  }
  copy(e, t) {
    return super.copy(e, t), this.color.copy(e.color), this.intensity = e.intensity, this;
  }
  toJSON(e) {
    const t = super.toJSON(e);
    return t.object.color = this.color.getHex(), t.object.intensity = this.intensity, t;
  }
}
const no = new Fe(), Yc = new P(), Kc = new P();
class $l {
  constructor(e) {
    this.camera = e, this.intensity = 1, this.bias = 0, this.biasNode = null, this.normalBias = 0, this.radius = 1, this.blurSamples = 8, this.mapSize = new We(512, 512), this.mapType = $t, this.map = null, this.mapPass = null, this.matrix = new Fe(), this.autoUpdate = !0, this.needsUpdate = !1, this._frustum = new Kl(), this._frameExtents = new We(1, 1), this._viewportCount = 1, this._viewports = [new gt(0, 0, 1, 1)];
  }
  getViewportCount() {
    return this._viewportCount;
  }
  getCamera() {
    return this.camera;
  }
  getFrustum() {
    return this._frustum;
  }
  updateMatrices(e) {
    const t = this.camera;
    Yc.setFromMatrixPosition(e.matrixWorld), t.position.copy(Yc), Kc.setFromMatrixPosition(e.target.matrixWorld), t.lookAt(Kc), t.updateMatrixWorld(), this._updateMatrix(t, this.matrix, this._frustum);
  }
  _updateMatrix(e, t, i, n) {
    no.multiplyMatrices(e.projectionMatrix, e.matrixWorldInverse), i.setFromProjectionMatrix(no, e.coordinateSystem, e.reversedDepth);
    const s = this._frameExtents, a = n ? n.z / s.x : 1, o = n ? n.w / s.y : 1, l = n ? n.x / s.x : 0, c = n ? n.y / s.y : 0;
    e.coordinateSystem === os || e.reversedDepth ? t.set(0.5 * a, 0, 0, 0.5 * a + l, 0, 0.5 * o, 0, 0.5 * o + c, 0, 0, 1, 0, 0, 0, 0, 1) : t.set(0.5 * a, 0, 0, 0.5 * a + l, 0, 0.5 * o, 0, 0.5 * o + c, 0, 0, 0.5, 0.5, 0, 0, 0, 1), t.multiply(no);
  }
  getViewport(e) {
    return this._viewports[e];
  }
  getFrameExtents() {
    return this._frameExtents;
  }
  dispose() {
    this.map && this.map.dispose(), this.mapPass && this.mapPass.dispose();
  }
  copy(e) {
    return this.camera = e.camera.clone(), this.intensity = e.intensity, this.bias = e.bias, this.radius = e.radius, this.autoUpdate = e.autoUpdate, this.needsUpdate = e.needsUpdate, this.normalBias = e.normalBias, this.blurSamples = e.blurSamples, this.mapSize.copy(e.mapSize), this.biasNode = e.biasNode, this;
  }
  clone() {
    return new this.constructor().copy(this);
  }
  toJSON() {
    const e = {};
    return e.intensity = this.intensity, e.bias = this.bias, e.normalBias = this.normalBias, e.radius = this.radius, e.blurSamples = this.blurSamples, e.mapSize = this.mapSize.toArray(), e.camera = this.camera.toJSON(!1).object, delete e.camera.matrix, e;
  }
}
const Vs = new P(), Hs = new Ce(), Ti = new P();
class dd extends at {
  constructor() {
    super(), this.isCamera = !0, this.type = "Camera", this.matrixWorldInverse = new Fe(), this.projectionMatrix = new Fe(), this.projectionMatrixInverse = new Fe(), this.coordinateSystem = Li, this._reversedDepth = !1;
  }
  get reversedDepth() {
    return this._reversedDepth;
  }
  copy(e, t) {
    return super.copy(e, t), this.matrixWorldInverse.copy(e.matrixWorldInverse), this.projectionMatrix.copy(e.projectionMatrix), this.projectionMatrixInverse.copy(e.projectionMatrixInverse), this.coordinateSystem = e.coordinateSystem, this;
  }
  getWorldDirection(e) {
    return super.getWorldDirection(e).negate();
  }
  updateMatrixWorld(e) {
    super.updateMatrixWorld(e), this.matrixWorld.decompose(Vs, Hs, Ti), Ti.x === 1 && Ti.y === 1 && Ti.z === 1 ? this.matrixWorldInverse.copy(this.matrixWorld).invert() : this.matrixWorldInverse.compose(Vs, Hs, Ti.set(1, 1, 1)).invert();
  }
  updateWorldMatrix(e, t, i = !1) {
    super.updateWorldMatrix(e, t, i), this.matrixWorld.decompose(Vs, Hs, Ti), Ti.x === 1 && Ti.y === 1 && Ti.z === 1 ? this.matrixWorldInverse.copy(this.matrixWorld).invert() : this.matrixWorldInverse.compose(Vs, Hs, Ti.set(1, 1, 1)).invert();
  }
  clone() {
    return new this.constructor().copy(this);
  }
}
const lr = new P(), Zc = new We(), $c = new We();
class zt extends dd {
  constructor(e = 50, t = 1, i = 0.1, n = 2e3) {
    super(), this.isPerspectiveCamera = !0, this.type = "PerspectiveCamera", this.fov = e, this.zoom = 1, this.near = i, this.far = n, this.focus = 10, this.aspect = t, this.view = null, this.filmGauge = 35, this.filmOffset = 0, this.updateProjectionMatrix();
  }
  copy(e, t) {
    return super.copy(e, t), this.fov = e.fov, this.zoom = e.zoom, this.near = e.near, this.far = e.far, this.focus = e.focus, this.aspect = e.aspect, this.view = e.view === null ? null : Object.assign({}, e.view), this.filmGauge = e.filmGauge, this.filmOffset = e.filmOffset, this;
  }
  setFocalLength(e) {
    const t = 0.5 * this.getFilmHeight() / e;
    this.fov = vn * 2 * Math.atan(t), this.updateProjectionMatrix();
  }
  getFocalLength() {
    const e = Math.tan(Jn * 0.5 * this.fov);
    return 0.5 * this.getFilmHeight() / e;
  }
  getEffectiveFOV() {
    return vn * 2 * Math.atan(Math.tan(Jn * 0.5 * this.fov) / this.zoom);
  }
  getFilmWidth() {
    return this.filmGauge * Math.min(this.aspect, 1);
  }
  getFilmHeight() {
    return this.filmGauge / Math.max(this.aspect, 1);
  }
  getViewBounds(e, t, i) {
    lr.set(-1, -1, 0.5).applyMatrix4(this.projectionMatrixInverse), t.set(lr.x, lr.y).multiplyScalar(-e / lr.z), lr.set(1, 1, 0.5).applyMatrix4(this.projectionMatrixInverse), i.set(lr.x, lr.y).multiplyScalar(-e / lr.z);
  }
  getViewSize(e, t) {
    return this.getViewBounds(e, Zc, $c), t.subVectors($c, Zc);
  }
  setViewOffset(e, t, i, n, s, a) {
    this.aspect = e / t, this.view === null && (this.view = { enabled: !0, fullWidth: 1, fullHeight: 1, offsetX: 0, offsetY: 0, width: 1, height: 1 }), this.view.enabled = !0, this.view.fullWidth = e, this.view.fullHeight = t, this.view.offsetX = i, this.view.offsetY = n, this.view.width = s, this.view.height = a, this.updateProjectionMatrix();
  }
  clearViewOffset() {
    this.view !== null && (this.view.enabled = !1), this.updateProjectionMatrix();
  }
  updateProjectionMatrix() {
    const e = this.near;
    let t = e * Math.tan(Jn * 0.5 * this.fov) / this.zoom, i = 2 * t, n = this.aspect * i, s = -0.5 * n;
    const a = this.view;
    if (this.view !== null && this.view.enabled) {
      const l = a.fullWidth, c = a.fullHeight;
      s += a.offsetX * n / l, t -= a.offsetY * i / c, n *= a.width / l, i *= a.height / c;
    }
    const o = this.filmOffset;
    o !== 0 && (s += e * o / this.getFilmWidth()), this.projectionMatrix.makePerspective(s, s + n, t, t - i, e, this.far, this.coordinateSystem, this.reversedDepth), this.projectionMatrixInverse.copy(this.projectionMatrix).invert();
  }
  toJSON(e) {
    const t = super.toJSON(e);
    return t.object.fov = this.fov, t.object.zoom = this.zoom, t.object.near = this.near, t.object.far = this.far, t.object.focus = this.focus, t.object.aspect = this.aspect, this.view !== null && (t.object.view = Object.assign({}, this.view)), t.object.filmGauge = this.filmGauge, t.object.filmOffset = this.filmOffset, t;
  }
}
class om extends $l {
  constructor() {
    super(new zt(50, 1, 0.5, 500)), this.isSpotLightShadow = !0, this.focus = 1, this.aspect = 1;
  }
  updateMatrices(e) {
    const t = this.camera, i = vn * 2 * e.angle * this.focus, n = this.mapSize.width / this.mapSize.height * this.aspect, s = e.distance || t.far;
    (i !== t.fov || n !== t.aspect || s !== t.far) && (t.fov = i, t.aspect = n, t.far = s, t.updateProjectionMatrix()), super.updateMatrices(e);
  }
  copy(e) {
    return super.copy(e), this.focus = e.focus, this.aspect = e.aspect, this;
  }
  toJSON() {
    const e = super.toJSON();
    return e.focus = this.focus, e.aspect = this.aspect, e;
  }
}
class lm extends Ta {
  constructor(e, t, i = 0, n = Math.PI / 3, s = 0, a = 2) {
    super(e, t), this.isSpotLight = !0, this.type = "SpotLight", this.position.copy(at.DEFAULT_UP), this.updateMatrix(), this.target = new at(), this.distance = i, this.angle = n, this.penumbra = s, this.decay = a, this.map = null, this.shadow = new om();
  }
  get power() {
    return this.intensity * Math.PI;
  }
  set power(e) {
    this.intensity = e / Math.PI;
  }
  dispose() {
    super.dispose(), this.shadow.dispose();
  }
  copy(e, t) {
    return super.copy(e, t), this.distance = e.distance, this.angle = e.angle, this.penumbra = e.penumbra, this.decay = e.decay, this.target = e.target.clone(), this.map = e.map, this.shadow = e.shadow.clone(), this;
  }
  toJSON(e) {
    const t = super.toJSON(e);
    return t.object.distance = this.distance, t.object.angle = this.angle, t.object.decay = this.decay, t.object.penumbra = this.penumbra, t.object.target = this.target.uuid, this.map && this.map.isTexture && (t.object.map = this.map.toJSON(e).uuid), t.object.shadow = this.shadow.toJSON(), t;
  }
}
class cm extends $l {
  constructor() {
    super(new zt(90, 1, 0.5, 500)), this.isPointLightShadow = !0;
  }
}
class hm extends Ta {
  constructor(e, t, i = 0, n = 2) {
    super(e, t), this.isPointLight = !0, this.type = "PointLight", this.distance = i, this.decay = n, this.shadow = new cm();
  }
  get power() {
    return this.intensity * 4 * Math.PI;
  }
  set power(e) {
    this.intensity = e / (4 * Math.PI);
  }
  dispose() {
    super.dispose(), this.shadow.dispose();
  }
  copy(e, t) {
    return super.copy(e, t), this.distance = e.distance, this.decay = e.decay, this.shadow = e.shadow.clone(), this;
  }
  toJSON(e) {
    const t = super.toJSON(e);
    return t.object.distance = this.distance, t.object.decay = this.decay, t.object.shadow = this.shadow.toJSON(), t;
  }
}
class Ea extends dd {
  constructor(e = -1, t = 1, i = 1, n = -1, s = 0.1, a = 2e3) {
    super(), this.isOrthographicCamera = !0, this.type = "OrthographicCamera", this.zoom = 1, this.view = null, this.left = e, this.right = t, this.top = i, this.bottom = n, this.near = s, this.far = a, this.updateProjectionMatrix();
  }
  copy(e, t) {
    return super.copy(e, t), this.left = e.left, this.right = e.right, this.top = e.top, this.bottom = e.bottom, this.near = e.near, this.far = e.far, this.zoom = e.zoom, this.view = e.view === null ? null : Object.assign({}, e.view), this;
  }
  setViewOffset(e, t, i, n, s, a) {
    this.view === null && (this.view = { enabled: !0, fullWidth: 1, fullHeight: 1, offsetX: 0, offsetY: 0, width: 1, height: 1 }), this.view.enabled = !0, this.view.fullWidth = e, this.view.fullHeight = t, this.view.offsetX = i, this.view.offsetY = n, this.view.width = s, this.view.height = a, this.updateProjectionMatrix();
  }
  clearViewOffset() {
    this.view !== null && (this.view.enabled = !1), this.updateProjectionMatrix();
  }
  updateProjectionMatrix() {
    const e = (this.right - this.left) / (2 * this.zoom), t = (this.top - this.bottom) / (2 * this.zoom), i = (this.right + this.left) / 2, n = (this.top + this.bottom) / 2;
    let s = i - e, a = i + e, o = n + t, l = n - t;
    if (this.view !== null && this.view.enabled) {
      const c = (this.right - this.left) / this.view.fullWidth / this.zoom, h = (this.top - this.bottom) / this.view.fullHeight / this.zoom;
      s += c * this.view.offsetX, a = s + c * this.view.width, o -= h * this.view.offsetY, l = o - h * this.view.height;
    }
    this.projectionMatrix.makeOrthographic(s, a, o, l, this.near, this.far, this.coordinateSystem, this.reversedDepth), this.projectionMatrixInverse.copy(this.projectionMatrix).invert();
  }
  toJSON(e) {
    const t = super.toJSON(e);
    return t.object.zoom = this.zoom, t.object.left = this.left, t.object.right = this.right, t.object.top = this.top, t.object.bottom = this.bottom, t.object.near = this.near, t.object.far = this.far, this.view !== null && (t.object.view = Object.assign({}, this.view)), t;
  }
}
class um extends $l {
  constructor() {
    super(new Ea(-5, 5, 5, -5, 0.5, 500)), this.isDirectionalLightShadow = !0;
  }
}
class yl extends Ta {
  constructor(e, t) {
    super(e, t), this.isDirectionalLight = !0, this.type = "DirectionalLight", this.position.copy(at.DEFAULT_UP), this.updateMatrix(), this.target = new at(), this.shadow = new um();
  }
  dispose() {
    super.dispose(), this.shadow.dispose();
  }
  copy(e) {
    return super.copy(e), this.target = e.target.clone(), this.shadow = e.shadow.clone(), this;
  }
  toJSON(e) {
    const t = super.toJSON(e);
    return t.object.shadow = this.shadow.toJSON(), t.object.target = this.target.uuid, t;
  }
}
class dm extends Ta {
  constructor(e, t) {
    super(e, t), this.isAmbientLight = !0, this.type = "AmbientLight";
  }
}
class ts {
  static extractUrlBase(e) {
    const t = e.lastIndexOf("/");
    return t === -1 ? "./" : e.slice(0, t + 1);
  }
  static resolveURL(e, t) {
    return typeof e != "string" || e === "" ? "" : (/^https?:\/\//i.test(t) && /^\//.test(e) && (t = t.replace(/(^https?:\/\/[^\/]+).*/i, "$1")), /^(https?:)?\/\//i.test(e) || /^data:.*,.*$/i.test(e) || /^blob:.*$/i.test(e) ? e : t + e);
  }
}
const so = /* @__PURE__ */ new WeakMap();
class pm extends En {
  constructor(e) {
    super(e), this.isImageBitmapLoader = !0, typeof createImageBitmap > "u" && Re("ImageBitmapLoader: createImageBitmap() not supported."), typeof fetch > "u" && Re("ImageBitmapLoader: fetch() not supported."), this.options = { premultiplyAlpha: "none" }, this._abortController = new AbortController();
  }
  setOptions(e) {
    return this.options = e, this;
  }
  load(e, t, i, n) {
    e === void 0 && (e = ""), this.path !== void 0 && (e = this.path + e), e = this.manager.resolveURL(e);
    const s = this, a = Zi.get(`image-bitmap:${e}`);
    if (a !== void 0) {
      if (s.manager.itemStart(e), a.then) {
        a.then((c) => {
          so.has(a) === !0 ? (n && n(so.get(a)), s.manager.itemError(e), s.manager.itemEnd(e)) : (t && t(c), s.manager.itemEnd(e));
        });
        return;
      }
      setTimeout(function() {
        t && t(a), s.manager.itemEnd(e);
      }, 0);
      return;
    }
    const o = {};
    o.credentials = this.crossOrigin === "anonymous" ? "same-origin" : "include", o.headers = this.requestHeader, o.signal = typeof AbortSignal.any == "function" ? AbortSignal.any([this._abortController.signal, this.manager.abortController.signal]) : this._abortController.signal;
    const l = fetch(e, o).then(function(c) {
      return c.blob();
    }).then(function(c) {
      return createImageBitmap(c, Object.assign({}, s.options, { colorSpaceConversion: "none" }));
    }).then(function(c) {
      return Zi.add(`image-bitmap:${e}`, c), t && t(c), s.manager.itemEnd(e), c;
    }).catch(function(c) {
      n && n(c), so.set(l, c), Zi.remove(`image-bitmap:${e}`), s.manager.itemError(e), s.manager.itemEnd(e);
    });
    Zi.add(`image-bitmap:${e}`, l), s.manager.itemStart(e);
  }
  abort() {
    return this._abortController.abort(), this._abortController = new AbortController(), this;
  }
}
const Zr = -90, $r = 1;
class fm extends at {
  constructor(e, t, i) {
    super(), this.type = "CubeCamera", this.renderTarget = i, this.coordinateSystem = null, this.activeMipmapLevel = 0;
    const n = new zt(Zr, $r, e, t);
    n.layers = this.layers, this.add(n);
    const s = new zt(Zr, $r, e, t);
    s.layers = this.layers, this.add(s);
    const a = new zt(Zr, $r, e, t);
    a.layers = this.layers, this.add(a);
    const o = new zt(Zr, $r, e, t);
    o.layers = this.layers, this.add(o);
    const l = new zt(Zr, $r, e, t);
    l.layers = this.layers, this.add(l);
    const c = new zt(Zr, $r, e, t);
    c.layers = this.layers, this.add(c);
  }
  updateCoordinateSystem() {
    const e = this.coordinateSystem, t = this.children.concat(), [i, n, s, a, o, l] = t;
    for (const c of t) this.remove(c);
    if (e === Li) i.up.set(0, 1, 0), i.lookAt(1, 0, 0), n.up.set(0, 1, 0), n.lookAt(-1, 0, 0), s.up.set(0, 0, -1), s.lookAt(0, 1, 0), a.up.set(0, 0, 1), a.lookAt(0, -1, 0), o.up.set(0, 1, 0), o.lookAt(0, 0, 1), l.up.set(0, 1, 0), l.lookAt(0, 0, -1);
    else if (e === os) i.up.set(0, -1, 0), i.lookAt(-1, 0, 0), n.up.set(0, -1, 0), n.lookAt(1, 0, 0), s.up.set(0, 0, 1), s.lookAt(0, 1, 0), a.up.set(0, 0, -1), a.lookAt(0, -1, 0), o.up.set(0, -1, 0), o.lookAt(0, 0, 1), l.up.set(0, -1, 0), l.lookAt(0, 0, -1);
    else throw new Error("THREE.CubeCamera.updateCoordinateSystem(): Invalid coordinate system: " + e);
    for (const c of t) this.add(c), c.updateMatrixWorld();
  }
  update(e, t) {
    this.parent === null && this.updateMatrixWorld();
    const { renderTarget: i, activeMipmapLevel: n } = this;
    this.coordinateSystem !== e.coordinateSystem && (this.coordinateSystem = e.coordinateSystem, this.updateCoordinateSystem());
    const [s, a, o, l, c, h] = this.children, d = e.getRenderTarget(), u = e.getActiveCubeFace(), p = e.getActiveMipmapLevel(), f = e.xr.enabled;
    e.xr.enabled = !1;
    const v = i.texture.generateMipmaps;
    i.texture.generateMipmaps = !1;
    let m = !1;
    e.isWebGLRenderer === !0 ? m = e.state.buffers.depth.getReversed() : m = e.reversedDepthBuffer, e.setRenderTarget(i, 0, n), m && e.autoClear === !1 && e.clearDepth(), e.render(t, s), e.setRenderTarget(i, 1, n), m && e.autoClear === !1 && e.clearDepth(), e.render(t, a), e.setRenderTarget(i, 2, n), m && e.autoClear === !1 && e.clearDepth(), e.render(t, o), e.setRenderTarget(i, 3, n), m && e.autoClear === !1 && e.clearDepth(), e.render(t, l), e.setRenderTarget(i, 4, n), m && e.autoClear === !1 && e.clearDepth(), e.render(t, c), i.texture.generateMipmaps = v, e.setRenderTarget(i, 5, n), m && e.autoClear === !1 && e.clearDepth(), e.render(t, h), e.setRenderTarget(d, u, p), e.xr.enabled = f, i.texture.needsPMREMUpdate = !0;
  }
}
class mm extends zt {
  constructor(e = []) {
    super(), this.isArrayCamera = !0, this.isMultiViewCamera = !1, this.cameras = e;
  }
}
class gm {
  constructor(e, t, i) {
    this.binding = e, this.valueSize = i;
    let n, s, a;
    switch (t) {
      case "quaternion":
        n = this._slerp, s = this._slerpAdditive, a = this._setAdditiveIdentityQuaternion, this.buffer = new Float64Array(i * 6), this._workIndex = 5;
        break;
      case "string":
      case "bool":
        n = this._select, s = this._select, a = this._setAdditiveIdentityOther, this.buffer = new Array(i * 5);
        break;
      default:
        n = this._lerp, s = this._lerpAdditive, a = this._setAdditiveIdentityNumeric, this.buffer = new Float64Array(i * 5);
    }
    this._mixBufferRegion = n, this._mixBufferRegionAdditive = s, this._setIdentity = a, this._origIndex = 3, this._addIndex = 4, this.cumulativeWeight = 0, this.cumulativeWeightAdditive = 0, this.useCount = 0, this.referenceCount = 0;
  }
  accumulate(e, t) {
    const i = this.buffer, n = this.valueSize, s = e * n + n;
    let a = this.cumulativeWeight;
    if (a === 0) {
      for (let o = 0; o !== n; ++o) i[s + o] = i[o];
      a = t;
    } else {
      a += t;
      const o = t / a;
      this._mixBufferRegion(i, s, 0, o, n);
    }
    this.cumulativeWeight = a;
  }
  accumulateAdditive(e) {
    const t = this.buffer, i = this.valueSize, n = i * this._addIndex;
    this.cumulativeWeightAdditive === 0 && this._setIdentity(), this._mixBufferRegionAdditive(t, n, 0, e, i), this.cumulativeWeightAdditive += e;
  }
  apply(e) {
    const t = this.valueSize, i = this.buffer, n = e * t + t, s = this.cumulativeWeight, a = this.cumulativeWeightAdditive, o = this.binding;
    if (this.cumulativeWeight = 0, this.cumulativeWeightAdditive = 0, s < 1) {
      const l = t * this._origIndex;
      this._mixBufferRegion(i, n, l, 1 - s, t);
    }
    a > 0 && this._mixBufferRegionAdditive(i, n, this._addIndex * t, 1, t);
    for (let l = t, c = t + t; l !== c; ++l) if (i[l] !== i[l + t]) {
      o.setValue(i, n);
      break;
    }
  }
  saveOriginalState() {
    const e = this.binding, t = this.buffer, i = this.valueSize, n = i * this._origIndex;
    e.getValue(t, n);
    for (let s = i, a = n; s !== a; ++s) t[s] = t[n + s % i];
    this._setIdentity(), this.cumulativeWeight = 0, this.cumulativeWeightAdditive = 0;
  }
  restoreOriginalState() {
    const e = this.valueSize * 3;
    this.binding.setValue(this.buffer, e);
  }
  _setAdditiveIdentityNumeric() {
    const e = this._addIndex * this.valueSize, t = e + this.valueSize;
    for (let i = e; i < t; i++) this.buffer[i] = 0;
  }
  _setAdditiveIdentityQuaternion() {
    this._setAdditiveIdentityNumeric(), this.buffer[this._addIndex * this.valueSize + 3] = 1;
  }
  _setAdditiveIdentityOther() {
    const e = this._origIndex * this.valueSize, t = this._addIndex * this.valueSize;
    for (let i = 0; i < this.valueSize; i++) this.buffer[t + i] = this.buffer[e + i];
  }
  _select(e, t, i, n, s) {
    if (n >= 0.5) for (let a = 0; a !== s; ++a) e[t + a] = e[i + a];
  }
  _slerp(e, t, i, n) {
    Ce.slerpFlat(e, t, e, t, e, i, n);
  }
  _slerpAdditive(e, t, i, n, s) {
    const a = this._workIndex * s;
    Ce.multiplyQuaternionsFlat(e, a, e, t, e, i), Ce.slerpFlat(e, t, e, t, e, a, n);
  }
  _lerp(e, t, i, n, s) {
    const a = 1 - n;
    for (let o = 0; o !== s; ++o) {
      const l = t + o;
      e[l] = e[l] * a + e[i + o] * n;
    }
  }
  _lerpAdditive(e, t, i, n, s) {
    for (let a = 0; a !== s; ++a) {
      const o = t + a;
      e[o] = e[o] + e[i + a] * n;
    }
  }
}
const Ql = "\\[\\]\\.:\\/", _m = new RegExp("[" + Ql + "]", "g"), Jl = "[^" + Ql + "]", vm = "[^" + Ql.replace("\\.", "") + "]", xm = /((?:WC+[\/:])*)/.source.replace("WC", Jl), Mm = /(WCOD+)?/.source.replace("WCOD", vm), ym = /(?:\.(WC+)(?:\[(.+)\])?)?/.source.replace("WC", Jl), Sm = /\.(WC+)(?:\[(.+)\])?/.source.replace("WC", Jl), Tm = new RegExp("^" + xm + Mm + ym + Sm + "$"), Em = ["material", "materials", "bones", "map"];
class bm {
  constructor(e, t, i) {
    const n = i || st.parseTrackName(t);
    this._targetGroup = e, this._bindings = e.subscribe_(t, n);
  }
  getValue(e, t) {
    this.bind();
    const i = this._targetGroup.nCachedObjects_, n = this._bindings[i];
    n !== void 0 && n.getValue(e, t);
  }
  setValue(e, t) {
    const i = this._bindings;
    for (let n = this._targetGroup.nCachedObjects_, s = i.length; n !== s; ++n) i[n].setValue(e, t);
  }
  bind() {
    const e = this._bindings;
    for (let t = this._targetGroup.nCachedObjects_, i = e.length; t !== i; ++t) e[t].bind();
  }
  unbind() {
    const e = this._bindings;
    for (let t = this._targetGroup.nCachedObjects_, i = e.length; t !== i; ++t) e[t].unbind();
  }
}
class st {
  constructor(e, t, i) {
    this.path = t, this.parsedPath = i || st.parseTrackName(t), this.node = st.findNode(e, this.parsedPath.nodeName), this.rootNode = e, this.getValue = this._getValue_unbound, this.setValue = this._setValue_unbound;
  }
  static create(e, t, i) {
    return e && e.isAnimationObjectGroup ? new st.Composite(e, t, i) : new st(e, t, i);
  }
  static sanitizeNodeName(e) {
    return e.replace(/\s/g, "_").replace(_m, "");
  }
  static parseTrackName(e) {
    const t = Tm.exec(e);
    if (t === null) throw new Error("THREE.PropertyBinding: Cannot parse trackName: " + e);
    const i = { nodeName: t[2], objectName: t[3], objectIndex: t[4], propertyName: t[5], propertyIndex: t[6] }, n = i.nodeName && i.nodeName.lastIndexOf(".");
    if (n !== void 0 && n !== -1) {
      const s = i.nodeName.substring(n + 1);
      Em.indexOf(s) !== -1 && (i.nodeName = i.nodeName.substring(0, n), i.objectName = s);
    }
    if (i.propertyName === null || i.propertyName.length === 0) throw new Error("THREE.PropertyBinding: can not parse propertyName from trackName: " + e);
    return i;
  }
  static findNode(e, t) {
    if (t === void 0 || t === "" || t === "." || t === -1 || t === e.name || t === e.uuid) return e;
    if (e.skeleton) {
      const i = e.skeleton.getBoneByName(t);
      if (i !== void 0) return i;
    }
    if (e.children) {
      const i = function(s) {
        for (let a = 0; a < s.length; a++) {
          const o = s[a];
          if (o.name === t || o.uuid === t) return o;
          const l = i(o.children);
          if (l) return l;
        }
        return null;
      }, n = i(e.children);
      if (n) return n;
    }
    return null;
  }
  _getValue_unavailable() {
  }
  _setValue_unavailable() {
  }
  _getValue_direct(e, t) {
    e[t] = this.targetObject[this.propertyName];
  }
  _getValue_array(e, t) {
    const i = this.resolvedProperty;
    for (let n = 0, s = i.length; n !== s; ++n) e[t++] = i[n];
  }
  _getValue_arrayElement(e, t) {
    e[t] = this.resolvedProperty[this.propertyIndex];
  }
  _getValue_toArray(e, t) {
    this.resolvedProperty.toArray(e, t);
  }
  _setValue_direct(e, t) {
    this.targetObject[this.propertyName] = e[t];
  }
  _setValue_direct_setNeedsUpdate(e, t) {
    this.targetObject[this.propertyName] = e[t], this.targetObject.needsUpdate = !0;
  }
  _setValue_direct_setMatrixWorldNeedsUpdate(e, t) {
    this.targetObject[this.propertyName] = e[t], this.targetObject.matrixWorldNeedsUpdate = !0;
  }
  _setValue_array(e, t) {
    const i = this.resolvedProperty;
    for (let n = 0, s = i.length; n !== s; ++n) i[n] = e[t++];
  }
  _setValue_array_setNeedsUpdate(e, t) {
    const i = this.resolvedProperty;
    for (let n = 0, s = i.length; n !== s; ++n) i[n] = e[t++];
    this.targetObject.needsUpdate = !0;
  }
  _setValue_array_setMatrixWorldNeedsUpdate(e, t) {
    const i = this.resolvedProperty;
    for (let n = 0, s = i.length; n !== s; ++n) i[n] = e[t++];
    this.targetObject.matrixWorldNeedsUpdate = !0;
  }
  _setValue_arrayElement(e, t) {
    this.resolvedProperty[this.propertyIndex] = e[t];
  }
  _setValue_arrayElement_setNeedsUpdate(e, t) {
    this.resolvedProperty[this.propertyIndex] = e[t], this.targetObject.needsUpdate = !0;
  }
  _setValue_arrayElement_setMatrixWorldNeedsUpdate(e, t) {
    this.resolvedProperty[this.propertyIndex] = e[t], this.targetObject.matrixWorldNeedsUpdate = !0;
  }
  _setValue_fromArray(e, t) {
    this.resolvedProperty.fromArray(e, t);
  }
  _setValue_fromArray_setNeedsUpdate(e, t) {
    this.resolvedProperty.fromArray(e, t), this.targetObject.needsUpdate = !0;
  }
  _setValue_fromArray_setMatrixWorldNeedsUpdate(e, t) {
    this.resolvedProperty.fromArray(e, t), this.targetObject.matrixWorldNeedsUpdate = !0;
  }
  _getValue_unbound(e, t) {
    this.bind(), this.getValue(e, t);
  }
  _setValue_unbound(e, t) {
    this.bind(), this.setValue(e, t);
  }
  bind() {
    let e = this.node;
    const t = this.parsedPath, i = t.objectName, n = t.propertyName;
    let s = t.propertyIndex;
    if (e || (e = st.findNode(this.rootNode, t.nodeName), this.node = e), this.getValue = this._getValue_unavailable, this.setValue = this._setValue_unavailable, !e) {
      Re("PropertyBinding: No target node found for track: " + this.path + ".");
      return;
    }
    if (i) {
      let c = t.objectIndex;
      switch (i) {
        case "materials":
          if (!e.material) {
            Be("PropertyBinding: Can not bind to material as node does not have a material.", this);
            return;
          }
          if (!e.material.materials) {
            Be("PropertyBinding: Can not bind to material.materials as node.material does not have a materials array.", this);
            return;
          }
          e = e.material.materials;
          break;
        case "bones":
          if (!e.skeleton) {
            Be("PropertyBinding: Can not bind to bones as node does not have a skeleton.", this);
            return;
          }
          e = e.skeleton.bones;
          for (let h = 0; h < e.length; h++) if (e[h].name === c) {
            c = h;
            break;
          }
          break;
        case "map":
          if ("map" in e) {
            e = e.map;
            break;
          }
          if (!e.material) {
            Be("PropertyBinding: Can not bind to material as node does not have a material.", this);
            return;
          }
          if (!e.material.map) {
            Be("PropertyBinding: Can not bind to material.map as node.material does not have a map.", this);
            return;
          }
          e = e.material.map;
          break;
        default:
          if (e[i] === void 0) {
            Be("PropertyBinding: Can not bind to objectName of node undefined.", this);
            return;
          }
          e = e[i];
      }
      if (c !== void 0) {
        if (e[c] === void 0) {
          Be("PropertyBinding: Trying to bind to objectIndex of objectName, but is undefined.", this, e);
          return;
        }
        e = e[c];
      }
    }
    const a = e[n];
    if (a === void 0) {
      const c = t.nodeName;
      Be("PropertyBinding: Trying to update property for track: " + c + "." + n + " but it wasn't found.", e);
      return;
    }
    let o = this.Versioning.None;
    this.targetObject = e, e.isMaterial === !0 ? o = this.Versioning.NeedsUpdate : e.isObject3D === !0 && (o = this.Versioning.MatrixWorldNeedsUpdate);
    let l = this.BindingType.Direct;
    if (s !== void 0) {
      if (n === "morphTargetInfluences") {
        if (!e.geometry) {
          Be("PropertyBinding: Can not bind to morphTargetInfluences because node does not have a geometry.", this);
          return;
        }
        if (!e.geometry.morphAttributes) {
          Be("PropertyBinding: Can not bind to morphTargetInfluences because node does not have a geometry.morphAttributes.", this);
          return;
        }
        e.morphTargetDictionary[s] !== void 0 && (s = e.morphTargetDictionary[s]);
      }
      l = this.BindingType.ArrayElement, this.resolvedProperty = a, this.propertyIndex = s;
    } else a.fromArray !== void 0 && a.toArray !== void 0 ? (l = this.BindingType.HasFromToArray, this.resolvedProperty = a) : Array.isArray(a) ? (l = this.BindingType.EntireArray, this.resolvedProperty = a) : this.propertyName = n;
    this.getValue = this.GetterByBindingType[l], this.setValue = this.SetterByBindingTypeAndVersioning[l][o];
  }
  unbind() {
    this.node = null, this.getValue = this._getValue_unbound, this.setValue = this._setValue_unbound;
  }
}
st.Composite = bm, st.prototype.BindingType = { Direct: 0, EntireArray: 1, ArrayElement: 2, HasFromToArray: 3 }, st.prototype.Versioning = { None: 0, NeedsUpdate: 1, MatrixWorldNeedsUpdate: 2 }, st.prototype.GetterByBindingType = [st.prototype._getValue_direct, st.prototype._getValue_array, st.prototype._getValue_arrayElement, st.prototype._getValue_toArray], st.prototype.SetterByBindingTypeAndVersioning = [[st.prototype._setValue_direct, st.prototype._setValue_direct_setNeedsUpdate, st.prototype._setValue_direct_setMatrixWorldNeedsUpdate], [st.prototype._setValue_array, st.prototype._setValue_array_setNeedsUpdate, st.prototype._setValue_array_setMatrixWorldNeedsUpdate], [st.prototype._setValue_arrayElement, st.prototype._setValue_arrayElement_setNeedsUpdate, st.prototype._setValue_arrayElement_setMatrixWorldNeedsUpdate], [st.prototype._setValue_fromArray, st.prototype._setValue_fromArray_setNeedsUpdate, st.prototype._setValue_fromArray_setMatrixWorldNeedsUpdate]];
class wm {
  constructor(e, t, i = null, n = t.blendMode) {
    this._mixer = e, this._clip = t, this._localRoot = i, this.blendMode = n;
    const s = t.tracks, a = s.length, o = new Array(a), l = { endingStart: hn, endingEnd: hn };
    for (let c = 0; c !== a; ++c) {
      const h = s[c].createInterpolant(null);
      o[c] = h, h.settings = l;
    }
    this._interpolantSettings = l, this._interpolants = o, this._propertyBindings = new Array(a), this._cacheIndex = null, this._byClipCacheIndex = null, this._timeScaleInterpolant = null, this._restoreTimeScale = null, this._weightInterpolant = null, this.loop = Kn, this._loopCount = -1, this._startTime = null, this.time = 0, this.timeScale = 1, this._effectiveTimeScale = 1, this.weight = 1, this._effectiveWeight = 1, this.repetitions = 1 / 0, this.paused = !1, this.enabled = !0, this.clampWhenFinished = !1, this.zeroSlopeAtStart = !0, this.zeroSlopeAtEnd = !0;
  }
  play() {
    return this._mixer._activateAction(this), this;
  }
  stop() {
    return this._mixer._deactivateAction(this), this.reset();
  }
  reset() {
    return this.paused = !1, this.enabled = !0, this.time = 0, this._loopCount = -1, this._startTime = null, this.stopFading().stopWarping();
  }
  isRunning() {
    return this.enabled && !this.paused && this.timeScale !== 0 && this._startTime === null && this._mixer._isActiveAction(this);
  }
  isScheduled() {
    return this._mixer._isActiveAction(this);
  }
  startAt(e) {
    return this._startTime = e, this;
  }
  setLoop(e, t) {
    return this.loop = e, this.repetitions = t, this;
  }
  setEffectiveWeight(e) {
    return this.weight = e, this._effectiveWeight = this.enabled ? e : 0, this.stopFading();
  }
  getEffectiveWeight() {
    return this._effectiveWeight;
  }
  fadeIn(e) {
    return this._scheduleFading(e, 0, 1);
  }
  fadeOut(e) {
    return this._scheduleFading(e, 1, 0);
  }
  crossFadeFrom(e, t, i = !1) {
    if (e.fadeOut(t), this.fadeIn(t), i === !0) {
      const n = this._clip.duration, s = e._clip.duration, a = s / n, o = n / s;
      e._restoreTimeScale = e.timeScale, this._restoreTimeScale = this.timeScale, e.warp(1, a, t), this.warp(o, 1, t);
    }
    return this;
  }
  crossFadeTo(e, t, i = !1) {
    return e.crossFadeFrom(this, t, i);
  }
  stopFading() {
    const e = this._weightInterpolant;
    return e !== null && (this._weightInterpolant = null, this._mixer._takeBackControlInterpolant(e)), this;
  }
  setEffectiveTimeScale(e) {
    return this.timeScale = e, this._effectiveTimeScale = this.paused ? 0 : e, this.stopWarping();
  }
  getEffectiveTimeScale() {
    return this._effectiveTimeScale;
  }
  setDuration(e) {
    return this.timeScale = this._clip.duration / e, this.stopWarping();
  }
  syncWith(e) {
    return this.time = e.time, this.timeScale = e.timeScale, this.stopWarping();
  }
  halt(e) {
    return this.warp(this._effectiveTimeScale, 0, e);
  }
  warp(e, t, i) {
    const n = this._mixer, s = n.time, a = this.timeScale;
    let o = this._timeScaleInterpolant;
    o === null && (o = n._lendControlInterpolant(), this._timeScaleInterpolant = o);
    const l = o.parameterPositions, c = o.sampleValues;
    return l[0] = s, l[1] = s + i, c[0] = e / a, c[1] = t / a, this;
  }
  stopWarping() {
    const e = this._timeScaleInterpolant;
    return e !== null && (this._timeScaleInterpolant = null, this._mixer._takeBackControlInterpolant(e)), this._restoreTimeScale = null, this;
  }
  getMixer() {
    return this._mixer;
  }
  getClip() {
    return this._clip;
  }
  getRoot() {
    return this._localRoot || this._mixer._root;
  }
  _update(e, t, i, n) {
    if (!this.enabled) {
      this._updateWeight(e);
      return;
    }
    const s = this._startTime;
    if (s !== null) {
      const l = (e - s) * i;
      l < 0 || i === 0 ? t = 0 : (this._startTime = null, t = i * l);
    }
    t *= this._updateTimeScale(e);
    const a = this._updateTime(t), o = this._updateWeight(e);
    if (o > 0) {
      const l = this._interpolants, c = this._propertyBindings;
      switch (this.blendMode) {
        case Np:
          for (let h = 0, d = l.length; h !== d; ++h) l[h].evaluate(a), c[h].accumulateAdditive(o);
          break;
        case zl:
        default:
          for (let h = 0, d = l.length; h !== d; ++h) l[h].evaluate(a), c[h].accumulate(n, o);
      }
    }
  }
  _updateWeight(e) {
    let t = 0;
    if (this.enabled) {
      t = this.weight;
      const i = this._weightInterpolant;
      if (i !== null) {
        const n = i.evaluate(e)[0];
        t *= n, e > i.parameterPositions[1] && (this.stopFading(), n === 0 && (this.enabled = !1));
      }
    }
    return this._effectiveWeight = t, t;
  }
  _updateTimeScale(e) {
    let t = 0;
    if (!this.paused) {
      t = this.timeScale;
      const i = this._timeScaleInterpolant;
      if (i !== null) {
        const n = i.evaluate(e)[0];
        t *= n, e > i.parameterPositions[1] && (t === 0 ? this.paused = !0 : (this._restoreTimeScale !== null && (t = this._restoreTimeScale), this.timeScale = t), this.stopWarping());
      }
    }
    return this._effectiveTimeScale = t, t;
  }
  _updateTime(e) {
    const t = this._clip.duration, i = this.loop;
    let n = this.time + e, s = this._loopCount;
    const a = i === Ip;
    if (e === 0) return s === -1 ? n : a && (s & 1) === 1 ? t - n : n;
    if (i === sn) {
      s === -1 && (this._loopCount = 0, this._setEndings(!0, !0, !1));
      e: {
        if (n >= t) n = t;
        else if (n < 0) n = 0;
        else {
          this.time = n;
          break e;
        }
        this.clampWhenFinished ? this.paused = !0 : this.enabled = !1, this.time = n, this._mixer.dispatchEvent({ type: "finished", action: this, direction: e < 0 ? -1 : 1 });
      }
    } else {
      if (s === -1 && (e >= 0 ? (s = 0, this._setEndings(!0, this.repetitions === 0, a)) : this._setEndings(this.repetitions === 0, !0, a)), n >= t || n < 0) {
        const o = Math.floor(n / t);
        n -= t * o, s += Math.abs(o);
        const l = this.repetitions - s;
        if (l <= 0) this.clampWhenFinished ? this.paused = !0 : this.enabled = !1, n = e > 0 ? t : 0, this.time = n, this._mixer.dispatchEvent({ type: "finished", action: this, direction: e > 0 ? 1 : -1 });
        else {
          if (l === 1) {
            const c = e < 0;
            this._setEndings(c, !c, a);
          } else this._setEndings(!1, !1, a);
          this._loopCount = s, this.time = n, this._mixer.dispatchEvent({ type: "loop", action: this, loopDelta: o });
        }
      } else this._loopCount = s, this.time = n;
      if (a && (s & 1) === 1) return t - n;
    }
    return n;
  }
  _setEndings(e, t, i) {
    const n = this._interpolantSettings;
    i ? (n.endingStart = un, n.endingEnd = un) : (e ? n.endingStart = this.zeroSlopeAtStart ? un : hn : n.endingStart = oa, t ? n.endingEnd = this.zeroSlopeAtEnd ? un : hn : n.endingEnd = oa);
  }
  _scheduleFading(e, t, i) {
    const n = this._mixer, s = n.time;
    let a = this._weightInterpolant;
    a === null && (a = n._lendControlInterpolant(), this._weightInterpolant = a);
    const o = a.parameterPositions, l = a.sampleValues;
    return o[0] = s, l[0] = t, o[1] = s + e, l[1] = i, this;
  }
}
const Am = new Float32Array(1);
class Rm extends mr {
  constructor(e) {
    super(), this._root = e, this._initMemoryManager(), this._accuIndex = 0, this.time = 0, this.timeScale = 1, typeof __THREE_DEVTOOLS__ < "u" && __THREE_DEVTOOLS__.dispatchEvent(new CustomEvent("observe", { detail: this }));
  }
  _bindAction(e, t) {
    const i = e._localRoot || this._root, n = e._clip.tracks, s = n.length, a = e._propertyBindings, o = e._interpolants, l = i.uuid, c = this._bindingsByRootAndName;
    let h = c[l];
    h === void 0 && (h = {}, c[l] = h);
    for (let d = 0; d !== s; ++d) {
      const u = n[d], p = u.name;
      let f = h[p];
      if (f !== void 0) ++f.referenceCount, a[d] = f;
      else {
        if (f = a[d], f !== void 0) {
          f._cacheIndex === null && (++f.referenceCount, this._addInactiveBinding(f, l, p));
          continue;
        }
        const v = t && t._propertyBindings[d].binding.parsedPath;
        f = new gm(st.create(i, p, v), u.ValueTypeName, u.getValueSize()), ++f.referenceCount, this._addInactiveBinding(f, l, p), a[d] = f;
      }
      o[d].resultBuffer = f.buffer;
    }
  }
  _activateAction(e) {
    if (!this._isActiveAction(e)) {
      if (e._cacheIndex === null) {
        const i = (e._localRoot || this._root).uuid, n = e._clip.uuid, s = this._actionsByClip[n];
        this._bindAction(e, s && s.knownActions[0]), this._addInactiveAction(e, n, i);
      }
      const t = e._propertyBindings;
      for (let i = 0, n = t.length; i !== n; ++i) {
        const s = t[i];
        s.useCount++ === 0 && (this._lendBinding(s), s.saveOriginalState());
      }
      this._lendAction(e);
    }
  }
  _deactivateAction(e) {
    if (this._isActiveAction(e)) {
      const t = e._propertyBindings;
      for (let i = 0, n = t.length; i !== n; ++i) {
        const s = t[i];
        --s.useCount === 0 && (s.restoreOriginalState(), this._takeBackBinding(s));
      }
      this._takeBackAction(e);
    }
  }
  _initMemoryManager() {
    this._actions = [], this._nActiveActions = 0, this._actionsByClip = {}, this._bindings = [], this._nActiveBindings = 0, this._bindingsByRootAndName = {}, this._controlInterpolants = [], this._nActiveControlInterpolants = 0;
    const e = this;
    this.stats = { actions: { get total() {
      return e._actions.length;
    }, get inUse() {
      return e._nActiveActions;
    } }, bindings: { get total() {
      return e._bindings.length;
    }, get inUse() {
      return e._nActiveBindings;
    } }, controlInterpolants: { get total() {
      return e._controlInterpolants.length;
    }, get inUse() {
      return e._nActiveControlInterpolants;
    } } };
  }
  _isActiveAction(e) {
    const t = e._cacheIndex;
    return t !== null && t < this._nActiveActions;
  }
  _addInactiveAction(e, t, i) {
    const n = this._actions, s = this._actionsByClip;
    let a = s[t];
    if (a === void 0) a = { knownActions: [e], actionByRoot: {} }, e._byClipCacheIndex = 0, s[t] = a;
    else {
      const o = a.knownActions;
      e._byClipCacheIndex = o.length, o.push(e);
    }
    e._cacheIndex = n.length, n.push(e), a.actionByRoot[i] = e;
  }
  _removeInactiveAction(e) {
    const t = this._actions, i = t[t.length - 1], n = e._cacheIndex;
    i._cacheIndex = n, t[n] = i, t.pop(), e._cacheIndex = null;
    const s = e._clip.uuid, a = this._actionsByClip, o = a[s], l = o.knownActions, c = l[l.length - 1], h = e._byClipCacheIndex;
    c._byClipCacheIndex = h, l[h] = c, l.pop(), e._byClipCacheIndex = null;
    const d = o.actionByRoot, u = (e._localRoot || this._root).uuid;
    delete d[u], l.length === 0 && delete a[s], this._removeInactiveBindingsForAction(e);
  }
  _removeInactiveBindingsForAction(e) {
    const t = e._propertyBindings;
    for (let i = 0, n = t.length; i !== n; ++i) {
      const s = t[i];
      --s.referenceCount === 0 && this._removeInactiveBinding(s);
    }
  }
  _lendAction(e) {
    const t = this._actions, i = e._cacheIndex, n = this._nActiveActions++, s = t[n];
    e._cacheIndex = n, t[n] = e, s._cacheIndex = i, t[i] = s;
  }
  _takeBackAction(e) {
    const t = this._actions, i = e._cacheIndex, n = --this._nActiveActions, s = t[n];
    e._cacheIndex = n, t[n] = e, s._cacheIndex = i, t[i] = s;
  }
  _addInactiveBinding(e, t, i) {
    const n = this._bindingsByRootAndName, s = this._bindings;
    let a = n[t];
    a === void 0 && (a = {}, n[t] = a), a[i] = e, e._cacheIndex = s.length, s.push(e);
  }
  _removeInactiveBinding(e) {
    const t = this._bindings, i = e.binding, n = i.rootNode.uuid, s = i.path, a = this._bindingsByRootAndName, o = a[n], l = t[t.length - 1], c = e._cacheIndex;
    l._cacheIndex = c, t[c] = l, t.pop(), delete o[s], Object.keys(o).length === 0 && delete a[n];
  }
  _lendBinding(e) {
    const t = this._bindings, i = e._cacheIndex, n = this._nActiveBindings++, s = t[n];
    e._cacheIndex = n, t[n] = e, s._cacheIndex = i, t[i] = s;
  }
  _takeBackBinding(e) {
    const t = this._bindings, i = e._cacheIndex, n = --this._nActiveBindings, s = t[n];
    e._cacheIndex = n, t[n] = e, s._cacheIndex = i, t[i] = s;
  }
  _lendControlInterpolant() {
    const e = this._controlInterpolants, t = this._nActiveControlInterpolants++;
    let i = e[t];
    return i === void 0 && (i = new ad(new Float32Array(2), new Float32Array(2), 1, Am), i.__cacheIndex = t, e[t] = i), i;
  }
  _takeBackControlInterpolant(e) {
    const t = this._controlInterpolants, i = e.__cacheIndex, n = --this._nActiveControlInterpolants, s = t[n];
    e.__cacheIndex = n, t[n] = e, s.__cacheIndex = i, t[i] = s;
  }
  clipAction(e, t, i) {
    const n = t || this._root, s = n.uuid;
    let a = typeof e == "string" ? hs.findByName(n, e) : e;
    const o = a !== null ? a.uuid : e, l = this._actionsByClip[o];
    let c = null;
    if (i === void 0 && (a !== null ? i = a.blendMode : i = zl), l !== void 0) {
      const d = l.actionByRoot[s];
      if (d !== void 0 && d.blendMode === i) return d;
      c = l.knownActions[0], a === null && (a = c._clip);
    }
    if (a === null) return null;
    const h = new wm(this, a, t, i);
    return this._bindAction(h, c), this._addInactiveAction(h, o, s), h;
  }
  existingAction(e, t) {
    const i = t || this._root, n = i.uuid, s = typeof e == "string" ? hs.findByName(i, e) : e, a = s ? s.uuid : e, o = this._actionsByClip[a];
    return o !== void 0 && o.actionByRoot[n] || null;
  }
  stopAllAction() {
    const e = this._actions, t = this._nActiveActions;
    for (let i = t - 1; i >= 0; --i) e[i].stop();
    return this;
  }
  update(e) {
    e *= this.timeScale;
    const t = this._actions, i = this._nActiveActions, n = this.time += e, s = Math.sign(e), a = this._accuIndex ^= 1;
    for (let c = 0; c !== i; ++c) t[c]._update(n, e, s, a);
    const o = this._bindings, l = this._nActiveBindings;
    for (let c = 0; c !== l; ++c) o[c].apply(a);
    return this;
  }
  setTime(e) {
    this.time = 0;
    for (let t = 0; t < this._actions.length; t++) this._actions[t].time = 0;
    return this.update(e);
  }
  getRoot() {
    return this._root;
  }
  uncacheClip(e) {
    const t = this._actions, i = e.uuid, n = this._actionsByClip, s = n[i];
    if (s !== void 0) {
      const a = s.knownActions;
      for (let o = 0, l = a.length; o !== l; ++o) {
        const c = a[o];
        this._deactivateAction(c);
        const h = c._cacheIndex, d = t[t.length - 1];
        c._cacheIndex = null, c._byClipCacheIndex = null, d._cacheIndex = h, t[h] = d, t.pop(), this._removeInactiveBindingsForAction(c);
      }
      delete n[i];
    }
  }
  uncacheRoot(e) {
    const t = e.uuid, i = this._actionsByClip;
    for (const a in i) {
      const o = i[a].actionByRoot, l = o[t];
      l !== void 0 && (this._deactivateAction(l), this._removeInactiveAction(l));
    }
    const n = this._bindingsByRootAndName, s = n[t];
    if (s !== void 0) for (const a in s) {
      const o = s[a];
      o.restoreOriginalState(), this._removeInactiveBinding(o);
    }
  }
  uncacheAction(e, t) {
    const i = this.existingAction(e, t);
    i !== null && (this._deactivateAction(i), this._removeInactiveAction(i));
  }
}
class Qc {
  constructor(e, t, i, n, s, a = !1) {
    this.isGLBufferAttribute = !0, this.name = "", this.buffer = e, this.type = t, this.itemSize = i, this.elementSize = n, this.count = s, this.normalized = a, this.version = 0;
  }
  set needsUpdate(e) {
    e === !0 && this.version++;
  }
  setBuffer(e) {
    return this.buffer = e, this;
  }
  setType(e, t) {
    return this.type = e, this.elementSize = t, this;
  }
  setItemSize(e) {
    return this.itemSize = e, this;
  }
  setCount(e) {
    return this.count = e, this;
  }
}
const Pm = class {
  constructor(e, t, i, n) {
    this.elements = [1, 0, 0, 1], e !== void 0 && this.set(e, t, i, n);
  }
  identity() {
    return this.set(1, 0, 0, 1), this;
  }
  fromArray(e, t = 0) {
    for (let i = 0; i < 4; i++) this.elements[i] = e[i + t];
    return this;
  }
  set(e, t, i, n) {
    const s = this.elements;
    return s[0] = e, s[2] = t, s[1] = i, s[3] = n, this;
  }
};
Pm.prototype.isMatrix2 = !0;
class Cm extends ds {
  constructor(e = 1) {
    const t = [0, 0, 0, e, 0, 0, 0, 0, 0, 0, e, 0, 0, 0, 0, 0, 0, e], i = [1, 0, 0, 1, 0.6, 0, 0, 1, 0, 0.6, 1, 0, 0, 0, 1, 0, 0.6, 1], n = new yt();
    n.setAttribute("position", new Jt(t, 3)), n.setAttribute("color", new Jt(i, 3));
    const s = new Dr({ vertexColors: !0, toneMapped: !1 });
    super(n, s), this.type = "AxesHelper";
  }
  setColors(e, t, i) {
    const n = new be(), s = this.geometry.attributes.color.array;
    return n.set(e), n.toArray(s, 0), n.toArray(s, 3), n.set(t), n.toArray(s, 6), n.toArray(s, 9), n.set(i), n.toArray(s, 12), n.toArray(s, 15), this.geometry.attributes.color.needsUpdate = !0, this;
  }
  dispose() {
    super.dispose(), this.geometry.dispose(), this.material.dispose();
  }
}
function Jc(r, e, t, i) {
  const n = Lm(i);
  switch (t) {
    case Fu:
      return r * e;
    case Bl:
      return r * e / n.components * n.byteLength;
    case Vl:
      return r * e / n.components * n.byteLength;
    case Ur:
      return r * e * 2 / n.components * n.byteLength;
    case Hl:
      return r * e * 2 / n.components * n.byteLength;
    case Bu:
      return r * e * 3 / n.components * n.byteLength;
    case li:
      return r * e * 4 / n.components * n.byteLength;
    case kl:
      return r * e * 4 / n.components * n.byteLength;
    case Ks:
    case Zs:
      return Math.floor((r + 3) / 4) * Math.floor((e + 3) / 4) * 8;
    case $s:
    case Qs:
      return Math.floor((r + 3) / 4) * Math.floor((e + 3) / 4) * 16;
    case Wo:
    case jo:
      return Math.max(r, 16) * Math.max(e, 8) / 4;
    case Go:
    case Xo:
      return Math.max(r, 8) * Math.max(e, 8) / 2;
    case qo:
    case Yo:
    case Zo:
    case $o:
      return Math.floor((r + 3) / 4) * Math.floor((e + 3) / 4) * 8;
    case Ko:
    case sa:
    case Qo:
      return Math.floor((r + 3) / 4) * Math.floor((e + 3) / 4) * 16;
    case Jo:
      return Math.floor((r + 3) / 4) * Math.floor((e + 3) / 4) * 16;
    case el:
      return Math.floor((r + 4) / 5) * Math.floor((e + 3) / 4) * 16;
    case tl:
      return Math.floor((r + 4) / 5) * Math.floor((e + 4) / 5) * 16;
    case il:
      return Math.floor((r + 5) / 6) * Math.floor((e + 4) / 5) * 16;
    case rl:
      return Math.floor((r + 5) / 6) * Math.floor((e + 5) / 6) * 16;
    case nl:
      return Math.floor((r + 7) / 8) * Math.floor((e + 4) / 5) * 16;
    case sl:
      return Math.floor((r + 7) / 8) * Math.floor((e + 5) / 6) * 16;
    case al:
      return Math.floor((r + 7) / 8) * Math.floor((e + 7) / 8) * 16;
    case ol:
      return Math.floor((r + 9) / 10) * Math.floor((e + 4) / 5) * 16;
    case ll:
      return Math.floor((r + 9) / 10) * Math.floor((e + 5) / 6) * 16;
    case cl:
      return Math.floor((r + 9) / 10) * Math.floor((e + 7) / 8) * 16;
    case hl:
      return Math.floor((r + 9) / 10) * Math.floor((e + 9) / 10) * 16;
    case ul:
      return Math.floor((r + 11) / 12) * Math.floor((e + 9) / 10) * 16;
    case dl:
      return Math.floor((r + 11) / 12) * Math.floor((e + 11) / 12) * 16;
    case pl:
    case fl:
    case ml:
      return Math.ceil(r / 4) * Math.ceil(e / 4) * 16;
    case gl:
    case _l:
      return Math.ceil(r / 4) * Math.ceil(e / 4) * 8;
    case aa:
    case vl:
      return Math.ceil(r / 4) * Math.ceil(e / 4) * 16;
  }
  throw new Error(`Unable to determine texture byte length for ${t} format.`);
}
function Lm(r) {
  switch (r) {
    case $t:
    case Nu:
      return { byteLength: 1, components: 1 };
    case rs:
    case Uu:
    case Ui:
      return { byteLength: 2, components: 1 };
    case Ol:
    case Fl:
      return { byteLength: 2, components: 4 };
    case Ni:
    case Dl:
    case oi:
      return { byteLength: 4, components: 1 };
    case Du:
    case Ou:
      return { byteLength: 4, components: 3 };
  }
  throw new Error(`THREE.TextureUtils: Unknown texture type ${r}.`);
}
typeof __THREE_DEVTOOLS__ < "u" && __THREE_DEVTOOLS__.dispatchEvent(new CustomEvent("register", { detail: { revision: Ir } })), typeof window < "u" && (window.__THREE__ ? Re("WARNING: Multiple instances of Three.js being imported.") : window.__THREE__ = Ir);
function pd() {
  let r = null, e = !1, t = null, i = null;
  function n(s, a) {
    i = r.requestAnimationFrame(n), t(s, a);
  }
  return { start: function() {
    e !== !0 && t !== null && r !== null && (i = r.requestAnimationFrame(n), e = !0);
  }, stop: function() {
    r !== null && r.cancelAnimationFrame(i), e = !1;
  }, setAnimationLoop: function(s) {
    t = s;
  }, setContext: function(s) {
    r = s;
  } };
}
function Im(r) {
  const e = /* @__PURE__ */ new WeakMap();
  function t(o, l) {
    const c = o.array, h = o.usage, d = c.byteLength, u = r.createBuffer();
    r.bindBuffer(l, u), r.bufferData(l, c, h), o.onUploadCallback();
    let p;
    if (c instanceof Float32Array) p = r.FLOAT;
    else if (typeof Float16Array < "u" && c instanceof Float16Array) p = r.HALF_FLOAT;
    else if (c instanceof Uint16Array) o.isFloat16BufferAttribute ? p = r.HALF_FLOAT : p = r.UNSIGNED_SHORT;
    else if (c instanceof Int16Array) p = r.SHORT;
    else if (c instanceof Uint32Array) p = r.UNSIGNED_INT;
    else if (c instanceof Int32Array) p = r.INT;
    else if (c instanceof Int8Array) p = r.BYTE;
    else if (c instanceof Uint8Array) p = r.UNSIGNED_BYTE;
    else if (c instanceof Uint8ClampedArray) p = r.UNSIGNED_BYTE;
    else throw new Error("THREE.WebGLAttributes: Unsupported buffer data format: " + c);
    return { buffer: u, type: p, bytesPerElement: c.BYTES_PER_ELEMENT, version: o.version, size: d };
  }
  function i(o, l, c) {
    const h = l.array, d = l.updateRanges;
    if (r.bindBuffer(c, o), d.length === 0) r.bufferSubData(c, 0, h);
    else {
      d.sort((p, f) => p.start - f.start);
      let u = 0;
      for (let p = 1; p < d.length; p++) {
        const f = d[u], v = d[p];
        v.start <= f.start + f.count + 1 ? f.count = Math.max(f.count, v.start + v.count - f.start) : (++u, d[u] = v);
      }
      d.length = u + 1;
      for (let p = 0, f = d.length; p < f; p++) {
        const v = d[p];
        r.bufferSubData(c, v.start * h.BYTES_PER_ELEMENT, h, v.start, v.count);
      }
      l.clearUpdateRanges();
    }
    l.onUploadCallback();
  }
  function n(o) {
    return o.isInterleavedBufferAttribute && (o = o.data), e.get(o);
  }
  function s(o) {
    o.isInterleavedBufferAttribute && (o = o.data);
    const l = e.get(o);
    l && (r.deleteBuffer(l.buffer), e.delete(o));
  }
  function a(o, l) {
    if (o.isInterleavedBufferAttribute && (o = o.data), o.isGLBufferAttribute) {
      const h = e.get(o);
      (!h || h.version < o.version) && e.set(o, { buffer: o.buffer, type: o.type, bytesPerElement: o.elementSize, version: o.version });
      return;
    }
    const c = e.get(o);
    if (c === void 0) e.set(o, t(o, l));
    else if (c.version < o.version) {
      if (c.size !== o.array.byteLength) throw new Error("THREE.WebGLAttributes: The size of the buffer attribute's array buffer does not match the original size. Resizing buffer attributes is not supported.");
      i(c.buffer, o, l), c.version = o.version;
    }
  }
  return { get: n, remove: s, update: a };
}
var Nm = `#ifdef USE_ALPHAHASH
	if ( diffuseColor.a < getAlphaHashThreshold( vPosition ) ) discard;
#endif`, Um = `#ifdef USE_ALPHAHASH
	const float ALPHA_HASH_SCALE = 0.05;
	float hash2D( vec2 value ) {
		return fract( 1.0e4 * sin( 17.0 * value.x + 0.1 * value.y ) * ( 0.1 + abs( sin( 13.0 * value.y + value.x ) ) ) );
	}
	float hash3D( vec3 value ) {
		return hash2D( vec2( hash2D( value.xy ), value.z ) );
	}
	float getAlphaHashThreshold( vec3 position ) {
		float maxDeriv = max(
			length( dFdx( position.xyz ) ),
			length( dFdy( position.xyz ) )
		);
		float pixScale = 1.0 / ( ALPHA_HASH_SCALE * maxDeriv );
		vec2 pixScales = vec2(
			exp2( floor( log2( pixScale ) ) ),
			exp2( ceil( log2( pixScale ) ) )
		);
		vec2 alpha = vec2(
			hash3D( floor( pixScales.x * position.xyz ) ),
			hash3D( floor( pixScales.y * position.xyz ) )
		);
		float lerpFactor = fract( log2( pixScale ) );
		float x = ( 1.0 - lerpFactor ) * alpha.x + lerpFactor * alpha.y;
		float a = min( lerpFactor, 1.0 - lerpFactor );
		vec3 cases = vec3(
			x * x / ( 2.0 * a * ( 1.0 - a ) ),
			( x - 0.5 * a ) / ( 1.0 - a ),
			1.0 - ( ( 1.0 - x ) * ( 1.0 - x ) / ( 2.0 * a * ( 1.0 - a ) ) )
		);
		float threshold = ( x < ( 1.0 - a ) )
			? ( ( x < a ) ? cases.x : cases.y )
			: cases.z;
		return clamp( threshold , 1.0e-6, 1.0 );
	}
#endif`, Dm = `#ifdef USE_ALPHAMAP
	diffuseColor.a *= texture2D( alphaMap, vAlphaMapUv ).g;
#endif`, Om = `#ifdef USE_ALPHAMAP
	uniform sampler2D alphaMap;
#endif`, Fm = `#ifdef USE_ALPHATEST
	#ifdef ALPHA_TO_COVERAGE
	diffuseColor.a = smoothstep( alphaTest, alphaTest + fwidth( diffuseColor.a ), diffuseColor.a );
	if ( diffuseColor.a == 0.0 ) discard;
	#else
	if ( diffuseColor.a < alphaTest ) discard;
	#endif
#endif`, Bm = `#ifdef USE_ALPHATEST
	uniform float alphaTest;
#endif`, Vm = `#ifdef USE_AOMAP
	float ambientOcclusion = ( texture2D( aoMap, vAoMapUv ).r - 1.0 ) * aoMapIntensity + 1.0;
	reflectedLight.indirectDiffuse *= ambientOcclusion;
	#if defined( USE_CLEARCOAT ) 
		clearcoatSpecularIndirect *= ambientOcclusion;
	#endif
	#if defined( USE_SHEEN ) 
		sheenSpecularIndirect *= ambientOcclusion;
	#endif
	#if defined( USE_ENVMAP ) && defined( STANDARD )
		float dotNV = saturate( dot( geometryNormal, geometryViewDir ) );
		reflectedLight.indirectSpecular *= computeSpecularOcclusion( dotNV, ambientOcclusion, material.roughness );
	#endif
#endif`, Hm = `#ifdef USE_AOMAP
	uniform sampler2D aoMap;
	uniform float aoMapIntensity;
#endif`, km = `#ifdef USE_BATCHING
	#if ! defined( GL_ANGLE_multi_draw )
	#define gl_DrawID _gl_DrawID
	uniform int _gl_DrawID;
	#endif
	uniform highp sampler2D batchingTexture;
	uniform highp usampler2D batchingIdTexture;
	mat4 getBatchingMatrix( const in float i ) {
		int size = textureSize( batchingTexture, 0 ).x;
		int j = int( i ) * 4;
		int x = j % size;
		int y = j / size;
		vec4 v1 = texelFetch( batchingTexture, ivec2( x, y ), 0 );
		vec4 v2 = texelFetch( batchingTexture, ivec2( x + 1, y ), 0 );
		vec4 v3 = texelFetch( batchingTexture, ivec2( x + 2, y ), 0 );
		vec4 v4 = texelFetch( batchingTexture, ivec2( x + 3, y ), 0 );
		return mat4( v1, v2, v3, v4 );
	}
	float getIndirectIndex( const in int i ) {
		int size = textureSize( batchingIdTexture, 0 ).x;
		int x = i % size;
		int y = i / size;
		return float( texelFetch( batchingIdTexture, ivec2( x, y ), 0 ).r );
	}
#endif
#ifdef USE_BATCHING_COLOR
	uniform sampler2D batchingColorTexture;
	vec4 getBatchingColor( const in float i ) {
		int size = textureSize( batchingColorTexture, 0 ).x;
		int j = int( i );
		int x = j % size;
		int y = j / size;
		return texelFetch( batchingColorTexture, ivec2( x, y ), 0 );
	}
#endif`, zm = `#ifdef USE_BATCHING
	mat4 batchingMatrix = getBatchingMatrix( getIndirectIndex( gl_DrawID ) );
#endif`, Gm = `vec3 transformed = vec3( position );
#ifdef USE_ALPHAHASH
	vPosition = vec3( position );
#endif`, Wm = `vec3 objectNormal = vec3( normal );
#ifdef USE_TANGENT
	vec3 objectTangent = vec3( tangent.xyz );
#endif`, Xm = `float G_BlinnPhong_Implicit( ) {
	return 0.25;
}
float D_BlinnPhong( const in float shininess, const in float dotNH ) {
	return RECIPROCAL_PI * ( shininess * 0.5 + 1.0 ) * pow( dotNH, shininess );
}
vec3 BRDF_BlinnPhong( const in vec3 lightDir, const in vec3 viewDir, const in vec3 normal, const in vec3 specularColor, const in float shininess ) {
	vec3 halfDir = normalize( lightDir + viewDir );
	float dotNH = saturate( dot( normal, halfDir ) );
	float dotVH = saturate( dot( viewDir, halfDir ) );
	vec3 F = F_Schlick( specularColor, 1.0, dotVH );
	float G = G_BlinnPhong_Implicit( );
	float D = D_BlinnPhong( shininess, dotNH );
	return F * ( G * D );
} // validated`, jm = `#ifdef USE_IRIDESCENCE
	const mat3 XYZ_TO_REC709 = mat3(
		 3.2404542, -0.9692660,  0.0556434,
		-1.5371385,  1.8760108, -0.2040259,
		-0.4985314,  0.0415560,  1.0572252
	);
	vec3 Fresnel0ToIor( vec3 fresnel0 ) {
		vec3 sqrtF0 = sqrt( fresnel0 );
		return ( vec3( 1.0 ) + sqrtF0 ) / ( vec3( 1.0 ) - sqrtF0 );
	}
	vec3 IorToFresnel0( vec3 transmittedIor, float incidentIor ) {
		return pow2( ( transmittedIor - vec3( incidentIor ) ) / ( transmittedIor + vec3( incidentIor ) ) );
	}
	float IorToFresnel0( float transmittedIor, float incidentIor ) {
		return pow2( ( transmittedIor - incidentIor ) / ( transmittedIor + incidentIor ));
	}
	vec3 evalSensitivity( float OPD, vec3 shift ) {
		float phase = 2.0 * PI * OPD * 1.0e-9;
		vec3 val = vec3( 5.4856e-13, 4.4201e-13, 5.2481e-13 );
		vec3 pos = vec3( 1.6810e+06, 1.7953e+06, 2.2084e+06 );
		vec3 var = vec3( 4.3278e+09, 9.3046e+09, 6.6121e+09 );
		vec3 xyz = val * sqrt( 2.0 * PI * var ) * cos( pos * phase + shift ) * exp( - pow2( phase ) * var );
		xyz.x += 9.7470e-14 * sqrt( 2.0 * PI * 4.5282e+09 ) * cos( 2.2399e+06 * phase + shift[ 0 ] ) * exp( - 4.5282e+09 * pow2( phase ) );
		xyz /= 1.0685e-7;
		vec3 rgb = XYZ_TO_REC709 * xyz;
		return rgb;
	}
	vec3 evalIridescence( float outsideIOR, float eta2, float cosTheta1, float thinFilmThickness, vec3 baseF0 ) {
		vec3 I;
		float iridescenceIOR = mix( outsideIOR, eta2, smoothstep( 0.0, 0.03, thinFilmThickness ) );
		float sinTheta2Sq = pow2( outsideIOR / iridescenceIOR ) * ( 1.0 - pow2( cosTheta1 ) );
		float cosTheta2Sq = 1.0 - sinTheta2Sq;
		if ( cosTheta2Sq < 0.0 ) {
			return vec3( 1.0 );
		}
		float cosTheta2 = sqrt( cosTheta2Sq );
		float R0 = IorToFresnel0( iridescenceIOR, outsideIOR );
		float R12 = F_Schlick( R0, 1.0, cosTheta1 );
		float T121 = 1.0 - R12;
		float phi12 = 0.0;
		if ( iridescenceIOR < outsideIOR ) phi12 = PI;
		float phi21 = PI - phi12;
		vec3 baseIOR = Fresnel0ToIor( clamp( baseF0, 0.0, 0.9999 ) );		vec3 R1 = IorToFresnel0( baseIOR, iridescenceIOR );
		vec3 R23 = F_Schlick( R1, 1.0, cosTheta2 );
		vec3 phi23 = vec3( 0.0 );
		if ( baseIOR[ 0 ] < iridescenceIOR ) phi23[ 0 ] = PI;
		if ( baseIOR[ 1 ] < iridescenceIOR ) phi23[ 1 ] = PI;
		if ( baseIOR[ 2 ] < iridescenceIOR ) phi23[ 2 ] = PI;
		float OPD = 2.0 * iridescenceIOR * thinFilmThickness * cosTheta2;
		vec3 phi = vec3( phi21 ) + phi23;
		vec3 R123 = clamp( R12 * R23, 1e-5, 0.9999 );
		vec3 r123 = sqrt( R123 );
		vec3 Rs = pow2( T121 ) * R23 / ( vec3( 1.0 ) - R123 );
		vec3 C0 = R12 + Rs;
		I = C0;
		vec3 Cm = Rs - T121;
		for ( int m = 1; m <= 2; ++ m ) {
			Cm *= r123;
			vec3 Sm = 2.0 * evalSensitivity( float( m ) * OPD, float( m ) * phi );
			I += Cm * Sm;
		}
		return max( I, vec3( 0.0 ) );
	}
#endif`, qm = `#ifdef USE_BUMPMAP
	uniform sampler2D bumpMap;
	uniform float bumpScale;
	vec2 dHdxy_fwd() {
		vec2 dSTdx = dFdx( vBumpMapUv );
		vec2 dSTdy = dFdy( vBumpMapUv );
		float Hll = bumpScale * texture2D( bumpMap, vBumpMapUv ).x;
		float dBx = bumpScale * texture2D( bumpMap, vBumpMapUv + dSTdx ).x - Hll;
		float dBy = bumpScale * texture2D( bumpMap, vBumpMapUv + dSTdy ).x - Hll;
		return vec2( dBx, dBy );
	}
	vec3 perturbNormalArb( vec3 surf_pos, vec3 surf_norm, vec2 dHdxy, float faceDirection ) {
		vec3 vSigmaX = normalize( dFdx( surf_pos.xyz ) );
		vec3 vSigmaY = normalize( dFdy( surf_pos.xyz ) );
		vec3 vN = surf_norm;
		vec3 R1 = cross( vSigmaY, vN );
		vec3 R2 = cross( vN, vSigmaX );
		float fDet = dot( vSigmaX, R1 ) * faceDirection;
		vec3 vGrad = sign( fDet ) * ( dHdxy.x * R1 + dHdxy.y * R2 );
		return normalize( abs( fDet ) * surf_norm - vGrad );
	}
#endif`, Ym = `#if NUM_CLIPPING_PLANES > 0
	vec4 plane;
	#ifdef ALPHA_TO_COVERAGE
		float distanceToPlane, distanceGradient;
		float clipOpacity = 1.0;
		#pragma unroll_loop_start
		for ( int i = 0; i < UNION_CLIPPING_PLANES; i ++ ) {
			plane = clippingPlanes[ i ];
			distanceToPlane = - dot( vClipPosition, plane.xyz ) + plane.w;
			distanceGradient = fwidth( distanceToPlane ) / 2.0;
			clipOpacity *= smoothstep( - distanceGradient, distanceGradient, distanceToPlane );
			if ( clipOpacity == 0.0 ) discard;
		}
		#pragma unroll_loop_end
		#if UNION_CLIPPING_PLANES < NUM_CLIPPING_PLANES
			float unionClipOpacity = 1.0;
			#pragma unroll_loop_start
			for ( int i = UNION_CLIPPING_PLANES; i < NUM_CLIPPING_PLANES; i ++ ) {
				plane = clippingPlanes[ i ];
				distanceToPlane = - dot( vClipPosition, plane.xyz ) + plane.w;
				distanceGradient = fwidth( distanceToPlane ) / 2.0;
				unionClipOpacity *= 1.0 - smoothstep( - distanceGradient, distanceGradient, distanceToPlane );
			}
			#pragma unroll_loop_end
			clipOpacity *= 1.0 - unionClipOpacity;
		#endif
		diffuseColor.a *= clipOpacity;
		if ( diffuseColor.a == 0.0 ) discard;
	#else
		#pragma unroll_loop_start
		for ( int i = 0; i < UNION_CLIPPING_PLANES; i ++ ) {
			plane = clippingPlanes[ i ];
			if ( dot( vClipPosition, plane.xyz ) > plane.w ) discard;
		}
		#pragma unroll_loop_end
		#if UNION_CLIPPING_PLANES < NUM_CLIPPING_PLANES
			bool clipped = true;
			#pragma unroll_loop_start
			for ( int i = UNION_CLIPPING_PLANES; i < NUM_CLIPPING_PLANES; i ++ ) {
				plane = clippingPlanes[ i ];
				clipped = ( dot( vClipPosition, plane.xyz ) > plane.w ) && clipped;
			}
			#pragma unroll_loop_end
			if ( clipped ) discard;
		#endif
	#endif
#endif`, Km = `#if NUM_CLIPPING_PLANES > 0
	varying vec3 vClipPosition;
	uniform vec4 clippingPlanes[ NUM_CLIPPING_PLANES ];
#endif`, Zm = `#if NUM_CLIPPING_PLANES > 0
	varying vec3 vClipPosition;
#endif`, $m = `#if NUM_CLIPPING_PLANES > 0
	vClipPosition = - mvPosition.xyz;
#endif`, Qm = `#if defined( USE_COLOR ) || defined( USE_COLOR_ALPHA )
	diffuseColor *= vColor;
#endif`, Jm = `#if defined( USE_COLOR ) || defined( USE_COLOR_ALPHA )
	varying vec4 vColor;
#endif`, eg = `#if defined( USE_COLOR ) || defined( USE_COLOR_ALPHA ) || defined( USE_INSTANCING_COLOR ) || defined( USE_BATCHING_COLOR )
	varying vec4 vColor;
#endif`, tg = `#if defined( USE_COLOR ) || defined( USE_COLOR_ALPHA ) || defined( USE_INSTANCING_COLOR ) || defined( USE_BATCHING_COLOR )
	vColor = vec4( 1.0 );
#endif
#ifdef USE_COLOR_ALPHA
	vColor *= color;
#elif defined( USE_COLOR )
	vColor.rgb *= color;
#endif
#ifdef USE_INSTANCING_COLOR
	vColor.rgb *= instanceColor.rgb;
#endif
#ifdef USE_BATCHING_COLOR
	vColor *= getBatchingColor( getIndirectIndex( gl_DrawID ) );
#endif`, ig = `#define PI 3.141592653589793
#define PI2 6.283185307179586
#define PI_HALF 1.5707963267948966
#define RECIPROCAL_PI 0.3183098861837907
#define RECIPROCAL_PI2 0.15915494309189535
#define EPSILON 1e-6
#ifndef saturate
#define saturate( a ) clamp( a, 0.0, 1.0 )
#endif
#define whiteComplement( a ) ( 1.0 - saturate( a ) )
float pow2( const in float x ) { return x*x; }
vec3 pow2( const in vec3 x ) { return x*x; }
float pow3( const in float x ) { return x*x*x; }
float pow4( const in float x ) { float x2 = x*x; return x2*x2; }
float max3( const in vec3 v ) { return max( max( v.x, v.y ), v.z ); }
float average( const in vec3 v ) { return dot( v, vec3( 0.3333333 ) ); }
highp float rand( const in vec2 uv ) {
	const highp float a = 12.9898, b = 78.233, c = 43758.5453;
	highp float dt = dot( uv.xy, vec2( a,b ) ), sn = mod( dt, PI );
	return fract( sin( sn ) * c );
}
#ifdef HIGH_PRECISION
	float precisionSafeLength( vec3 v ) { return length( v ); }
#else
	float precisionSafeLength( vec3 v ) {
		float maxComponent = max3( abs( v ) );
		return length( v / maxComponent ) * maxComponent;
	}
#endif
struct IncidentLight {
	vec3 color;
	vec3 direction;
	bool visible;
};
struct ReflectedLight {
	vec3 directDiffuse;
	vec3 directSpecular;
	vec3 indirectDiffuse;
	vec3 indirectSpecular;
};
#ifdef USE_ALPHAHASH
	varying vec3 vPosition;
#endif
vec3 transformDirection( in vec3 dir, in mat4 matrix ) {
	return normalize( ( matrix * vec4( dir, 0.0 ) ).xyz );
}
#define inverseTransformDirection transformDirectionByInverseViewMatrix
vec3 transformNormalByInverseViewMatrix( in vec3 normal, in mat4 viewMatrix ) {
	return normalize( ( vec4( normal, 0.0 ) * viewMatrix ).xyz );
}
vec3 transformDirectionByInverseViewMatrix( in vec3 dir, in mat4 viewMatrix ) {
	return normalize( ( vec4( dir, 0.0 ) * viewMatrix ).xyz );
}
bool isPerspectiveMatrix( mat4 m ) {
	return m[ 2 ][ 3 ] == - 1.0;
}
vec2 equirectUv( in vec3 dir ) {
	float u = atan( dir.z, dir.x ) * RECIPROCAL_PI2 + 0.5;
	float v = asin( clamp( dir.y, - 1.0, 1.0 ) ) * RECIPROCAL_PI + 0.5;
	return vec2( u, v );
}
vec3 BRDF_Lambert( const in vec3 diffuseColor ) {
	return RECIPROCAL_PI * diffuseColor;
}
vec3 F_Schlick( const in vec3 f0, const in float f90, const in float dotVH ) {
	float fresnel = exp2( ( - 5.55473 * dotVH - 6.98316 ) * dotVH );
	return f0 * ( 1.0 - fresnel ) + ( f90 * fresnel );
}
float F_Schlick( const in float f0, const in float f90, const in float dotVH ) {
	float fresnel = exp2( ( - 5.55473 * dotVH - 6.98316 ) * dotVH );
	return f0 * ( 1.0 - fresnel ) + ( f90 * fresnel );
} // validated`, rg = `#ifdef ENVMAP_TYPE_CUBE_UV
	#define cubeUV_minMipLevel 4.0
	#define cubeUV_minTileSize 16.0
	float getFace( vec3 direction ) {
		vec3 absDirection = abs( direction );
		float face = - 1.0;
		if ( absDirection.x > absDirection.z ) {
			if ( absDirection.x > absDirection.y )
				face = direction.x > 0.0 ? 0.0 : 3.0;
			else
				face = direction.y > 0.0 ? 1.0 : 4.0;
		} else {
			if ( absDirection.z > absDirection.y )
				face = direction.z > 0.0 ? 2.0 : 5.0;
			else
				face = direction.y > 0.0 ? 1.0 : 4.0;
		}
		return face;
	}
	vec2 getUV( vec3 direction, float face ) {
		vec2 uv;
		if ( face == 0.0 ) {
			uv = vec2( direction.z, direction.y ) / abs( direction.x );
		} else if ( face == 1.0 ) {
			uv = vec2( - direction.x, - direction.z ) / abs( direction.y );
		} else if ( face == 2.0 ) {
			uv = vec2( - direction.x, direction.y ) / abs( direction.z );
		} else if ( face == 3.0 ) {
			uv = vec2( - direction.z, direction.y ) / abs( direction.x );
		} else if ( face == 4.0 ) {
			uv = vec2( - direction.x, direction.z ) / abs( direction.y );
		} else {
			uv = vec2( direction.x, direction.y ) / abs( direction.z );
		}
		return 0.5 * ( uv + 1.0 );
	}
	vec3 bilinearCubeUV( sampler2D envMap, vec3 direction, float mipInt ) {
		float face = getFace( direction );
		float filterInt = max( cubeUV_minMipLevel - mipInt, 0.0 );
		mipInt = max( mipInt, cubeUV_minMipLevel );
		float faceSize = exp2( mipInt );
		highp vec2 uv = getUV( direction, face ) * ( faceSize - 2.0 ) + 1.0;
		if ( face > 2.0 ) {
			uv.y += faceSize;
			face -= 3.0;
		}
		uv.x += face * faceSize;
		uv.x += filterInt * 3.0 * cubeUV_minTileSize;
		uv.y += 4.0 * ( exp2( CUBEUV_MAX_MIP ) - faceSize );
		uv.x *= CUBEUV_TEXEL_WIDTH;
		uv.y *= CUBEUV_TEXEL_HEIGHT;
		#ifdef texture2DGradEXT
			return texture2DGradEXT( envMap, uv, vec2( 0.0 ), vec2( 0.0 ) ).rgb;
		#else
			return texture2D( envMap, uv ).rgb;
		#endif
	}
	#define cubeUV_r0 1.0
	#define cubeUV_m0 - 2.0
	#define cubeUV_r1 0.8
	#define cubeUV_m1 - 1.0
	#define cubeUV_r4 0.4
	#define cubeUV_m4 2.0
	#define cubeUV_r5 0.305
	#define cubeUV_m5 3.0
	#define cubeUV_r6 0.21
	#define cubeUV_m6 4.0
	float roughnessToMip( float roughness ) {
		float mip = 0.0;
		if ( roughness >= cubeUV_r1 ) {
			mip = ( cubeUV_r0 - roughness ) * ( cubeUV_m1 - cubeUV_m0 ) / ( cubeUV_r0 - cubeUV_r1 ) + cubeUV_m0;
		} else if ( roughness >= cubeUV_r4 ) {
			mip = ( cubeUV_r1 - roughness ) * ( cubeUV_m4 - cubeUV_m1 ) / ( cubeUV_r1 - cubeUV_r4 ) + cubeUV_m1;
		} else if ( roughness >= cubeUV_r5 ) {
			mip = ( cubeUV_r4 - roughness ) * ( cubeUV_m5 - cubeUV_m4 ) / ( cubeUV_r4 - cubeUV_r5 ) + cubeUV_m4;
		} else if ( roughness >= cubeUV_r6 ) {
			mip = ( cubeUV_r5 - roughness ) * ( cubeUV_m6 - cubeUV_m5 ) / ( cubeUV_r5 - cubeUV_r6 ) + cubeUV_m5;
		} else {
			mip = - 2.0 * log2( 1.16 * roughness );		}
		return mip;
	}
	vec4 textureCubeUV( sampler2D envMap, vec3 sampleDir, float roughness ) {
		float mip = clamp( roughnessToMip( roughness ), cubeUV_m0, CUBEUV_MAX_MIP );
		float mipF = fract( mip );
		float mipInt = floor( mip );
		vec3 color0 = bilinearCubeUV( envMap, sampleDir, mipInt );
		if ( mipF == 0.0 ) {
			return vec4( color0, 1.0 );
		} else {
			vec3 color1 = bilinearCubeUV( envMap, sampleDir, mipInt + 1.0 );
			return vec4( mix( color0, color1, mipF ), 1.0 );
		}
	}
#endif`, ng = `vec3 transformedNormal = objectNormal;
#ifdef USE_TANGENT
	vec3 transformedTangent = objectTangent;
#endif
#ifdef USE_BATCHING
	mat3 bm = mat3( batchingMatrix );
	transformedNormal /= vec3( dot( bm[ 0 ], bm[ 0 ] ), dot( bm[ 1 ], bm[ 1 ] ), dot( bm[ 2 ], bm[ 2 ] ) );
	transformedNormal = bm * transformedNormal;
	#ifdef USE_TANGENT
		transformedTangent = bm * transformedTangent;
	#endif
#endif
#ifdef USE_INSTANCING
	mat3 im = mat3( instanceMatrix );
	transformedNormal /= vec3( dot( im[ 0 ], im[ 0 ] ), dot( im[ 1 ], im[ 1 ] ), dot( im[ 2 ], im[ 2 ] ) );
	transformedNormal = im * transformedNormal;
	#ifdef USE_TANGENT
		transformedTangent = im * transformedTangent;
	#endif
#endif
transformedNormal = normalMatrix * transformedNormal;
#ifdef FLIP_SIDED
	transformedNormal = - transformedNormal;
#endif
#ifdef USE_TANGENT
	transformedTangent = ( modelViewMatrix * vec4( transformedTangent, 0.0 ) ).xyz;
#endif`, sg = `#ifdef USE_DISPLACEMENTMAP
	uniform sampler2D displacementMap;
	uniform float displacementScale;
	uniform float displacementBias;
#endif`, ag = `#ifdef USE_DISPLACEMENTMAP
	transformed += normalize( objectNormal ) * ( texture2D( displacementMap, vDisplacementMapUv ).x * displacementScale + displacementBias );
#endif`, og = `#ifdef USE_EMISSIVEMAP
	vec4 emissiveColor = texture2D( emissiveMap, vEmissiveMapUv );
	#ifdef DECODE_VIDEO_TEXTURE_EMISSIVE
		emissiveColor = sRGBTransferEOTF( emissiveColor );
	#endif
	totalEmissiveRadiance *= emissiveColor.rgb;
#endif`, lg = `#ifdef USE_EMISSIVEMAP
	uniform sampler2D emissiveMap;
#endif`, cg = "gl_FragColor = linearToOutputTexel( gl_FragColor );", hg = `vec4 LinearTransferOETF( in vec4 value ) {
	return value;
}
vec4 sRGBTransferEOTF( in vec4 value ) {
	return vec4( mix( pow( value.rgb * 0.9478672986 + vec3( 0.0521327014 ), vec3( 2.4 ) ), value.rgb * 0.0773993808, vec3( lessThanEqual( value.rgb, vec3( 0.04045 ) ) ) ), value.a );
}
vec4 sRGBTransferOETF( in vec4 value ) {
	return vec4( mix( pow( value.rgb, vec3( 0.41666 ) ) * 1.055 - vec3( 0.055 ), value.rgb * 12.92, vec3( lessThanEqual( value.rgb, vec3( 0.0031308 ) ) ) ), value.a );
}`, ug = `#ifdef USE_ENVMAP
	#ifdef ENV_WORLDPOS
		vec3 cameraToFrag;
		if ( isOrthographic ) {
			cameraToFrag = normalize( vec3( - viewMatrix[ 0 ][ 2 ], - viewMatrix[ 1 ][ 2 ], - viewMatrix[ 2 ][ 2 ] ) );
		} else {
			cameraToFrag = normalize( vWorldPosition - cameraPosition );
		}
		vec3 worldNormal = transformNormalByInverseViewMatrix( normal, viewMatrix );
		#ifdef ENVMAP_MODE_REFLECTION
			vec3 reflectVec = reflect( cameraToFrag, worldNormal );
		#else
			vec3 reflectVec = refract( cameraToFrag, worldNormal, refractionRatio );
		#endif
	#else
		vec3 reflectVec = vReflect;
	#endif
	#ifdef ENVMAP_TYPE_CUBE
		vec4 envColor = textureCube( envMap, envMapRotation * reflectVec );
		#ifdef ENVMAP_BLENDING_MULTIPLY
			outgoingLight = mix( outgoingLight, outgoingLight * envColor.xyz, specularStrength * reflectivity );
		#elif defined( ENVMAP_BLENDING_MIX )
			outgoingLight = mix( outgoingLight, envColor.xyz, specularStrength * reflectivity );
		#elif defined( ENVMAP_BLENDING_ADD )
			outgoingLight += envColor.xyz * specularStrength * reflectivity;
		#endif
	#endif
#endif`, dg = `#ifdef USE_ENVMAP
	uniform float envMapIntensity;
	uniform mat3 envMapRotation;
	#ifdef ENVMAP_TYPE_CUBE
		uniform samplerCube envMap;
	#else
		uniform sampler2D envMap;
	#endif
#endif`, pg = `#ifdef USE_ENVMAP
	uniform float reflectivity;
	#if defined( USE_BUMPMAP ) || defined( USE_NORMALMAP ) || defined( PHONG ) || defined( LAMBERT )
		#define ENV_WORLDPOS
	#endif
	#ifdef ENV_WORLDPOS
		varying vec3 vWorldPosition;
		uniform float refractionRatio;
	#else
		varying vec3 vReflect;
	#endif
#endif`, fg = `#ifdef USE_ENVMAP
	#if defined( USE_BUMPMAP ) || defined( USE_NORMALMAP ) || defined( PHONG ) || defined( LAMBERT )
		#define ENV_WORLDPOS
	#endif
	#ifdef ENV_WORLDPOS
		
		varying vec3 vWorldPosition;
	#else
		varying vec3 vReflect;
		uniform float refractionRatio;
	#endif
#endif`, mg = `#ifdef USE_ENVMAP
	#ifdef ENV_WORLDPOS
		vWorldPosition = worldPosition.xyz;
	#else
		vec3 cameraToVertex;
		if ( isOrthographic ) {
			cameraToVertex = normalize( vec3( - viewMatrix[ 0 ][ 2 ], - viewMatrix[ 1 ][ 2 ], - viewMatrix[ 2 ][ 2 ] ) );
		} else {
			cameraToVertex = normalize( worldPosition.xyz - cameraPosition );
		}
		vec3 worldNormal = transformNormalByInverseViewMatrix( transformedNormal, viewMatrix );
		#ifdef ENVMAP_MODE_REFLECTION
			vReflect = reflect( cameraToVertex, worldNormal );
		#else
			vReflect = refract( cameraToVertex, worldNormal, refractionRatio );
		#endif
	#endif
#endif`, gg = `#ifdef USE_FOG
	vFogDepth = - mvPosition.z;
#endif`, _g = `#ifdef USE_FOG
	varying float vFogDepth;
#endif`, vg = `#ifdef USE_FOG
	#ifdef FOG_EXP2
		float fogFactor = 1.0 - exp( - fogDensity * fogDensity * vFogDepth * vFogDepth );
	#else
		float fogFactor = smoothstep( fogNear, fogFar, vFogDepth );
	#endif
	gl_FragColor.rgb = mix( gl_FragColor.rgb, fogColor, fogFactor );
#endif`, xg = `#ifdef USE_FOG
	uniform vec3 fogColor;
	varying float vFogDepth;
	#ifdef FOG_EXP2
		uniform float fogDensity;
	#else
		uniform float fogNear;
		uniform float fogFar;
	#endif
#endif`, Mg = `#ifdef USE_GRADIENTMAP
	uniform sampler2D gradientMap;
#endif
vec3 getGradientIrradiance( vec3 normal, vec3 lightDirection ) {
	float dotNL = dot( normal, lightDirection );
	vec2 coord = vec2( dotNL * 0.5 + 0.5, 0.0 );
	#ifdef USE_GRADIENTMAP
		return vec3( texture2D( gradientMap, coord ).r );
	#else
		vec2 fw = fwidth( coord ) * 0.5;
		return mix( vec3( 0.7 ), vec3( 1.0 ), smoothstep( 0.7 - fw.x, 0.7 + fw.x, coord.x ) );
	#endif
}`, yg = `#ifdef USE_LIGHTMAP
	uniform sampler2D lightMap;
	uniform float lightMapIntensity;
#endif`, Sg = `LambertMaterial material;
material.diffuseColor = diffuseColor.rgb;
material.specularStrength = specularStrength;`, Tg = `varying vec3 vViewPosition;
struct LambertMaterial {
	vec3 diffuseColor;
	float specularStrength;
};
void RE_Direct_Lambert( const in IncidentLight directLight, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in LambertMaterial material, inout ReflectedLight reflectedLight ) {
	float dotNL = saturate( dot( geometryNormal, directLight.direction ) );
	vec3 irradiance = dotNL * directLight.color;
	reflectedLight.directDiffuse += irradiance * BRDF_Lambert( material.diffuseColor );
}
void RE_IndirectDiffuse_Lambert( const in vec3 irradiance, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in LambertMaterial material, inout ReflectedLight reflectedLight ) {
	reflectedLight.indirectDiffuse += irradiance * BRDF_Lambert( material.diffuseColor );
}
#define RE_Direct				RE_Direct_Lambert
#define RE_IndirectDiffuse		RE_IndirectDiffuse_Lambert`, Eg = `uniform bool receiveShadow;
uniform vec3 ambientLightColor;
#if defined( USE_LIGHT_PROBES )
	uniform vec3 lightProbe[ 9 ];
#endif
vec3 shGetIrradianceAt( in vec3 normal, in vec3 shCoefficients[ 9 ] ) {
	float x = normal.x, y = normal.y, z = normal.z;
	vec3 result = shCoefficients[ 0 ] * 0.886227;
	result += shCoefficients[ 1 ] * 2.0 * 0.511664 * y;
	result += shCoefficients[ 2 ] * 2.0 * 0.511664 * z;
	result += shCoefficients[ 3 ] * 2.0 * 0.511664 * x;
	result += shCoefficients[ 4 ] * 2.0 * 0.429043 * x * y;
	result += shCoefficients[ 5 ] * 2.0 * 0.429043 * y * z;
	result += shCoefficients[ 6 ] * ( 0.743125 * z * z - 0.247708 );
	result += shCoefficients[ 7 ] * 2.0 * 0.429043 * x * z;
	result += shCoefficients[ 8 ] * 0.429043 * ( x * x - y * y );
	return result;
}
vec3 getLightProbeIrradiance( const in vec3 lightProbe[ 9 ], const in vec3 normal ) {
	vec3 worldNormal = transformNormalByInverseViewMatrix( normal, viewMatrix );
	vec3 irradiance = shGetIrradianceAt( worldNormal, lightProbe );
	return irradiance;
}
vec3 getAmbientLightIrradiance( const in vec3 ambientLightColor ) {
	vec3 irradiance = ambientLightColor;
	return irradiance;
}
float getDistanceAttenuation( const in float lightDistance, const in float cutoffDistance, const in float decayExponent ) {
	float distanceFalloff = 1.0 / max( pow( lightDistance, decayExponent ), 0.01 );
	if ( cutoffDistance > 0.0 ) {
		distanceFalloff *= pow2( saturate( 1.0 - pow4( lightDistance / cutoffDistance ) ) );
	}
	return distanceFalloff;
}
float getSpotAttenuation( const in float coneCosine, const in float penumbraCosine, const in float angleCosine ) {
	return smoothstep( coneCosine, penumbraCosine, angleCosine );
}
#if NUM_SUN_LIGHTS > 0
	struct SunLight {
		vec3 direction;
		vec3 color;
	};
	uniform SunLight sunLights[ NUM_SUN_LIGHTS ];
	void getSunLightInfo( const in SunLight sunLight, out IncidentLight light ) {
		light.color = sunLight.color;
		light.direction = sunLight.direction;
		light.visible = true;
	}
#endif
#if NUM_DIR_LIGHTS > 0
	struct DirectionalLight {
		vec3 direction;
		vec3 color;
	};
	uniform DirectionalLight directionalLights[ NUM_DIR_LIGHTS ];
	void getDirectionalLightInfo( const in DirectionalLight directionalLight, out IncidentLight light ) {
		light.color = directionalLight.color;
		light.direction = directionalLight.direction;
		light.visible = true;
	}
#endif
#if NUM_POINT_LIGHTS > 0
	struct PointLight {
		vec3 position;
		vec3 color;
		float distance;
		float decay;
	};
	uniform PointLight pointLights[ NUM_POINT_LIGHTS ];
	void getPointLightInfo( const in PointLight pointLight, const in vec3 geometryPosition, out IncidentLight light ) {
		vec3 lVector = pointLight.position - geometryPosition;
		light.direction = normalize( lVector );
		float lightDistance = length( lVector );
		light.color = pointLight.color;
		light.color *= getDistanceAttenuation( lightDistance, pointLight.distance, pointLight.decay );
		light.visible = ( light.color != vec3( 0.0 ) );
	}
#endif
#if NUM_SPOT_LIGHTS > 0
	struct SpotLight {
		vec3 position;
		vec3 direction;
		vec3 color;
		float distance;
		float decay;
		float coneCos;
		float penumbraCos;
	};
	uniform SpotLight spotLights[ NUM_SPOT_LIGHTS ];
	void getSpotLightInfo( const in SpotLight spotLight, const in vec3 geometryPosition, out IncidentLight light ) {
		vec3 lVector = spotLight.position - geometryPosition;
		light.direction = normalize( lVector );
		float angleCos = dot( light.direction, spotLight.direction );
		float spotAttenuation = getSpotAttenuation( spotLight.coneCos, spotLight.penumbraCos, angleCos );
		if ( spotAttenuation > 0.0 ) {
			float lightDistance = length( lVector );
			light.color = spotLight.color * spotAttenuation;
			light.color *= getDistanceAttenuation( lightDistance, spotLight.distance, spotLight.decay );
			light.visible = ( light.color != vec3( 0.0 ) );
		} else {
			light.color = vec3( 0.0 );
			light.visible = false;
		}
	}
#endif
#if NUM_RECT_AREA_LIGHTS > 0
	struct RectAreaLight {
		vec3 color;
		vec3 position;
		vec3 halfWidth;
		vec3 halfHeight;
	};
	uniform sampler2D ltc_1;	uniform sampler2D ltc_2;
	uniform RectAreaLight rectAreaLights[ NUM_RECT_AREA_LIGHTS ];
#endif
#if NUM_HEMI_LIGHTS > 0
	struct HemisphereLight {
		vec3 direction;
		vec3 skyColor;
		vec3 groundColor;
	};
	uniform HemisphereLight hemisphereLights[ NUM_HEMI_LIGHTS ];
	vec3 getHemisphereLightIrradiance( const in HemisphereLight hemiLight, const in vec3 normal ) {
		float dotNL = dot( normal, hemiLight.direction );
		float hemiDiffuseWeight = 0.5 * dotNL + 0.5;
		vec3 irradiance = mix( hemiLight.groundColor, hemiLight.skyColor, hemiDiffuseWeight );
		return irradiance;
	}
#endif
#include <lightprobes_pars_fragment>`, bg = `#ifdef USE_ENVMAP
	vec3 getIBLIrradiance( const in vec3 normal ) {
		#ifdef ENVMAP_TYPE_CUBE_UV
			vec3 worldNormal = transformNormalByInverseViewMatrix( normal, viewMatrix );
			vec4 envMapColor = textureCubeUV( envMap, envMapRotation * worldNormal, 1.0 );
			return PI * envMapColor.rgb * envMapIntensity;
		#else
			return vec3( 0.0 );
		#endif
	}
	vec3 getIBLRadiance( const in vec3 viewDir, const in vec3 normal, const in float roughness ) {
		#ifdef ENVMAP_TYPE_CUBE_UV
			vec3 reflectVec = reflect( - viewDir, normal );
			reflectVec = normalize( mix( reflectVec, normal, pow4( roughness ) ) );
			reflectVec = transformDirectionByInverseViewMatrix( reflectVec, viewMatrix );
			vec4 envMapColor = textureCubeUV( envMap, envMapRotation * reflectVec, roughness );
			return envMapColor.rgb * envMapIntensity;
		#else
			return vec3( 0.0 );
		#endif
	}
	#ifdef USE_RETROREFLECTION
		vec3 getIBLRetroRadiance( const in vec3 viewDir, const in vec3 normal, const in float roughness ) {
			#ifdef ENVMAP_TYPE_CUBE_UV
				vec3 retroVec = normalize( mix( viewDir, normal, pow4( roughness ) ) );
				retroVec = transformDirectionByInverseViewMatrix( retroVec, viewMatrix );
				vec4 envMapColor = textureCubeUV( envMap, envMapRotation * retroVec, roughness );
				return envMapColor.rgb * envMapIntensity;
			#else
				return vec3( 0.0 );
			#endif
		}
	#endif
	#ifdef USE_ANISOTROPY
		vec3 getIBLAnisotropyRadiance( const in vec3 viewDir, const in vec3 normal, const in float roughness, const in vec3 bitangent, const in float anisotropy ) {
			#ifdef ENVMAP_TYPE_CUBE_UV
				vec3 bentNormal = cross( bitangent, viewDir );
				bentNormal = normalize( cross( bentNormal, bitangent ) );
				bentNormal = normalize( mix( bentNormal, normal, pow2( pow2( 1.0 - anisotropy * ( 1.0 - roughness ) ) ) ) );
				return getIBLRadiance( viewDir, bentNormal, roughness );
			#else
				return vec3( 0.0 );
			#endif
		}
		#ifdef USE_RETROREFLECTION
			vec3 getIBLAnisotropyRetroRadiance( const in vec3 viewDir, const in vec3 normal, const in float roughness, const in vec3 bitangent, const in float anisotropy ) {
				#ifdef ENVMAP_TYPE_CUBE_UV
					vec3 bentNormal = cross( bitangent, viewDir );
					bentNormal = normalize( cross( bentNormal, bitangent ) );
					bentNormal = normalize( mix( bentNormal, normal, pow2( pow2( 1.0 - anisotropy * ( 1.0 - roughness ) ) ) ) );
					return getIBLRetroRadiance( viewDir, bentNormal, roughness );
				#else
					return vec3( 0.0 );
				#endif
			}
		#endif
	#endif
#endif`, wg = `ToonMaterial material;
material.diffuseColor = diffuseColor.rgb;`, Ag = `varying vec3 vViewPosition;
struct ToonMaterial {
	vec3 diffuseColor;
};
void RE_Direct_Toon( const in IncidentLight directLight, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in ToonMaterial material, inout ReflectedLight reflectedLight ) {
	vec3 irradiance = getGradientIrradiance( geometryNormal, directLight.direction ) * directLight.color;
	reflectedLight.directDiffuse += irradiance * BRDF_Lambert( material.diffuseColor );
}
void RE_IndirectDiffuse_Toon( const in vec3 irradiance, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in ToonMaterial material, inout ReflectedLight reflectedLight ) {
	reflectedLight.indirectDiffuse += irradiance * BRDF_Lambert( material.diffuseColor );
}
#define RE_Direct				RE_Direct_Toon
#define RE_IndirectDiffuse		RE_IndirectDiffuse_Toon`, Rg = `BlinnPhongMaterial material;
material.diffuseColor = diffuseColor.rgb;
material.specularColor = specular;
material.specularShininess = shininess;
material.specularStrength = specularStrength;`, Pg = `varying vec3 vViewPosition;
struct BlinnPhongMaterial {
	vec3 diffuseColor;
	vec3 specularColor;
	float specularShininess;
	float specularStrength;
};
void RE_Direct_BlinnPhong( const in IncidentLight directLight, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in BlinnPhongMaterial material, inout ReflectedLight reflectedLight ) {
	float dotNL = saturate( dot( geometryNormal, directLight.direction ) );
	vec3 irradiance = dotNL * directLight.color;
	reflectedLight.directDiffuse += irradiance * BRDF_Lambert( material.diffuseColor );
	reflectedLight.directSpecular += irradiance * BRDF_BlinnPhong( directLight.direction, geometryViewDir, geometryNormal, material.specularColor, material.specularShininess ) * material.specularStrength;
}
void RE_IndirectDiffuse_BlinnPhong( const in vec3 irradiance, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in BlinnPhongMaterial material, inout ReflectedLight reflectedLight ) {
	reflectedLight.indirectDiffuse += irradiance * BRDF_Lambert( material.diffuseColor );
}
#define RE_Direct				RE_Direct_BlinnPhong
#define RE_IndirectDiffuse		RE_IndirectDiffuse_BlinnPhong`, Cg = `PhysicalMaterial material;
material.diffuseColor = diffuseColor.rgb;
material.diffuseContribution = diffuseColor.rgb * ( 1.0 - metalnessFactor );
material.metalness = metalnessFactor;
vec3 dxy = max( abs( dFdx( nonPerturbedNormal ) ), abs( dFdy( nonPerturbedNormal ) ) );
float geometryRoughness = max( max( dxy.x, dxy.y ), dxy.z );
material.roughness = max( roughnessFactor, 0.0525 );material.roughness += geometryRoughness;
material.roughness = min( material.roughness, 1.0 );
#ifdef IOR
	material.ior = ior;
	#ifdef USE_SPECULAR
		float specularIntensityFactor = specularIntensity;
		vec3 specularColorFactor = specularColor;
		#ifdef USE_SPECULAR_COLORMAP
			specularColorFactor *= texture2D( specularColorMap, vSpecularColorMapUv ).rgb;
		#endif
		#ifdef USE_SPECULAR_INTENSITYMAP
			specularIntensityFactor *= texture2D( specularIntensityMap, vSpecularIntensityMapUv ).a;
		#endif
		material.specularF90 = mix( specularIntensityFactor, 1.0, metalnessFactor );
	#else
		float specularIntensityFactor = 1.0;
		vec3 specularColorFactor = vec3( 1.0 );
		material.specularF90 = 1.0;
	#endif
	material.specularColor = min( pow2( ( material.ior - 1.0 ) / ( material.ior + 1.0 ) ) * specularColorFactor, vec3( 1.0 ) ) * specularIntensityFactor;
	material.specularColorBlended = mix( material.specularColor, diffuseColor.rgb, metalnessFactor );
#else
	material.specularColor = vec3( 0.04 );
	material.specularColorBlended = mix( material.specularColor, diffuseColor.rgb, metalnessFactor );
	material.specularF90 = 1.0;
#endif
#ifdef USE_CLEARCOAT
	material.clearcoat = clearcoat;
	material.clearcoatRoughness = clearcoatRoughness;
	material.clearcoatF0 = vec3( 0.04 );
	material.clearcoatF90 = 1.0;
	#ifdef USE_CLEARCOATMAP
		material.clearcoat *= texture2D( clearcoatMap, vClearcoatMapUv ).x;
	#endif
	#ifdef USE_CLEARCOAT_ROUGHNESSMAP
		material.clearcoatRoughness *= texture2D( clearcoatRoughnessMap, vClearcoatRoughnessMapUv ).y;
	#endif
	material.clearcoat = saturate( material.clearcoat );	material.clearcoatRoughness = max( material.clearcoatRoughness, 0.0525 );
	material.clearcoatRoughness += geometryRoughness;
	material.clearcoatRoughness = min( material.clearcoatRoughness, 1.0 );
#endif
#ifdef USE_DISPERSION
	material.dispersion = dispersion;
#endif
#ifdef USE_RETROREFLECTION
	material.retroreflectivity = retroreflectivity;
#endif
#ifdef USE_IRIDESCENCE
	material.iridescence = iridescence;
	material.iridescenceIOR = iridescenceIOR;
	#ifdef USE_IRIDESCENCEMAP
		material.iridescence *= texture2D( iridescenceMap, vIridescenceMapUv ).r;
	#endif
	#ifdef USE_IRIDESCENCE_THICKNESSMAP
		material.iridescenceThickness = (iridescenceThicknessMaximum - iridescenceThicknessMinimum) * texture2D( iridescenceThicknessMap, vIridescenceThicknessMapUv ).g + iridescenceThicknessMinimum;
	#else
		material.iridescenceThickness = iridescenceThicknessMaximum;
	#endif
#endif
#ifdef USE_SHEEN
	material.sheenColor = sheenColor;
	#ifdef USE_SHEEN_COLORMAP
		material.sheenColor *= texture2D( sheenColorMap, vSheenColorMapUv ).rgb;
	#endif
	material.sheenRoughness = clamp( sheenRoughness, 0.0001, 1.0 );
	#ifdef USE_SHEEN_ROUGHNESSMAP
		material.sheenRoughness *= texture2D( sheenRoughnessMap, vSheenRoughnessMapUv ).a;
	#endif
#endif
#ifdef USE_ANISOTROPY
	#ifdef USE_ANISOTROPYMAP
		mat2 anisotropyMat = mat2( anisotropyVector.x, anisotropyVector.y, - anisotropyVector.y, anisotropyVector.x );
		vec3 anisotropyPolar = texture2D( anisotropyMap, vAnisotropyMapUv ).rgb;
		vec2 anisotropyV = anisotropyMat * normalize( 2.0 * anisotropyPolar.rg - vec2( 1.0 ) ) * anisotropyPolar.b;
	#else
		vec2 anisotropyV = anisotropyVector;
	#endif
	material.anisotropy = length( anisotropyV );
	if( material.anisotropy == 0.0 ) {
		anisotropyV = vec2( 1.0, 0.0 );
	} else {
		anisotropyV /= material.anisotropy;
		material.anisotropy = saturate( material.anisotropy );
	}
	material.alphaT = mix( pow2( material.roughness ), 1.0, pow2( material.anisotropy ) );
	material.anisotropyT = tbn[ 0 ] * anisotropyV.x + tbn[ 1 ] * anisotropyV.y;
	material.anisotropyB = tbn[ 1 ] * anisotropyV.x - tbn[ 0 ] * anisotropyV.y;
#endif`, Lg = `uniform sampler2D dfgLUT;
struct PhysicalMaterial {
	vec3 diffuseColor;
	vec3 diffuseContribution;
	vec3 specularColor;
	vec3 specularColorBlended;
	float roughness;
	float metalness;
	float specularF90;
	float dispersion;
	vec2 dfg;
	vec3 multiScatteringCompensation;
	#ifdef USE_RETROREFLECTION
		float retroreflectivity;
	#endif
	#ifdef USE_CLEARCOAT
		float clearcoat;
		float clearcoatRoughness;
		vec3 clearcoatF0;
		float clearcoatF90;
	#endif
	#ifdef USE_IRIDESCENCE
		float iridescence;
		float iridescenceIOR;
		float iridescenceThickness;
		vec3 iridescenceFresnel;
		vec3 iridescenceF0Dielectric;
		vec3 iridescenceF0Metallic;
	#endif
	#ifdef USE_SHEEN
		vec3 sheenColor;
		float sheenRoughness;
	#endif
	#ifdef IOR
		float ior;
	#endif
	#ifdef USE_TRANSMISSION
		float transmission;
		float transmissionAlpha;
		float thickness;
		float attenuationDistance;
		vec3 attenuationColor;
	#endif
	#ifdef USE_ANISOTROPY
		float anisotropy;
		float alphaT;
		vec3 anisotropyT;
		vec3 anisotropyB;
	#endif
};
vec3 clearcoatSpecularDirect = vec3( 0.0 );
vec3 clearcoatSpecularIndirect = vec3( 0.0 );
vec3 sheenSpecularDirect = vec3( 0.0 );
vec3 sheenSpecularIndirect = vec3(0.0 );
vec3 Schlick_to_F0( const in vec3 f, const in float f90, const in float dotVH ) {
    float x = clamp( 1.0 - dotVH, 0.0, 1.0 );
    float x2 = x * x;
    float x5 = clamp( x * x2 * x2, 0.0, 0.9999 );
    return ( f - vec3( f90 ) * x5 ) / ( 1.0 - x5 );
}
float V_GGX_SmithCorrelated( const in float alpha, const in float dotNL, const in float dotNV ) {
	float a2 = pow2( alpha );
	float gv = dotNL * sqrt( a2 + ( 1.0 - a2 ) * pow2( dotNV ) );
	float gl = dotNV * sqrt( a2 + ( 1.0 - a2 ) * pow2( dotNL ) );
	return 0.5 / max( gv + gl, EPSILON );
}
float D_GGX( const in float alpha, const in float dotNH ) {
	float a2 = pow2( alpha );
	float denom = pow2( dotNH ) * ( a2 - 1.0 ) + 1.0;
	return RECIPROCAL_PI * a2 / pow2( denom );
}
#ifdef USE_ANISOTROPY
	float V_GGX_SmithCorrelated_Anisotropic( const in float alphaT, const in float alphaB, const in float dotTV, const in float dotBV, const in float dotTL, const in float dotBL, const in float dotNV, const in float dotNL ) {
		float gv = dotNL * length( vec3( alphaT * dotTV, alphaB * dotBV, dotNV ) );
		float gl = dotNV * length( vec3( alphaT * dotTL, alphaB * dotBL, dotNL ) );
		return 0.5 / max( gv + gl, EPSILON );
	}
	float D_GGX_Anisotropic( const in float alphaT, const in float alphaB, const in float dotNH, const in float dotTH, const in float dotBH ) {
		float a2 = alphaT * alphaB;
		highp vec3 v = vec3( alphaB * dotTH, alphaT * dotBH, a2 * dotNH );
		highp float v2 = dot( v, v );
		float w2 = a2 / v2;
		return RECIPROCAL_PI * a2 * pow2 ( w2 );
	}
#endif
#ifdef USE_CLEARCOAT
	vec3 BRDF_GGX_Clearcoat( const in vec3 lightDir, const in vec3 viewDir, const in vec3 normal, const in PhysicalMaterial material) {
		vec3 f0 = material.clearcoatF0;
		float f90 = material.clearcoatF90;
		float roughness = material.clearcoatRoughness;
		float alpha = pow2( roughness );
		vec3 halfDir = normalize( lightDir + viewDir );
		float dotNL = saturate( dot( normal, lightDir ) );
		float dotNV = saturate( dot( normal, viewDir ) );
		float dotNH = saturate( dot( normal, halfDir ) );
		float dotVH = saturate( dot( viewDir, halfDir ) );
		vec3 F = F_Schlick( f0, f90, dotVH );
		float V = V_GGX_SmithCorrelated( alpha, dotNL, dotNV );
		float D = D_GGX( alpha, dotNH );
		return F * ( V * D );
	}
#endif
vec3 BRDF_GGX( const in vec3 lightDir, const in vec3 viewDir, const in vec3 normal, const in PhysicalMaterial material ) {
	vec3 f0 = material.specularColorBlended;
	float f90 = material.specularF90;
	float roughness = material.roughness;
	float alpha = pow2( roughness );
	vec3 halfDir = normalize( lightDir + viewDir );
	float dotNL = saturate( dot( normal, lightDir ) );
	float dotNV = saturate( dot( normal, viewDir ) );
	float dotNH = saturate( dot( normal, halfDir ) );
	float dotVH = saturate( dot( viewDir, halfDir ) );
	vec3 F = F_Schlick( f0, f90, dotVH );
	#ifdef USE_IRIDESCENCE
		F = mix( F, material.iridescenceFresnel, material.iridescence );
	#endif
	#ifdef USE_ANISOTROPY
		float dotTL = dot( material.anisotropyT, lightDir );
		float dotTV = dot( material.anisotropyT, viewDir );
		float dotTH = dot( material.anisotropyT, halfDir );
		float dotBL = dot( material.anisotropyB, lightDir );
		float dotBV = dot( material.anisotropyB, viewDir );
		float dotBH = dot( material.anisotropyB, halfDir );
		float V = V_GGX_SmithCorrelated_Anisotropic( material.alphaT, alpha, dotTV, dotBV, dotTL, dotBL, dotNV, dotNL );
		float D = D_GGX_Anisotropic( material.alphaT, alpha, dotNH, dotTH, dotBH );
	#else
		float V = V_GGX_SmithCorrelated( alpha, dotNL, dotNV );
		float D = D_GGX( alpha, dotNH );
	#endif
	return F * ( V * D );
}
vec2 LTC_Uv( const in vec3 N, const in vec3 V, const in float roughness ) {
	const float LUT_SIZE = 64.0;
	const float LUT_SCALE = ( LUT_SIZE - 1.0 ) / LUT_SIZE;
	const float LUT_BIAS = 0.5 / LUT_SIZE;
	float dotNV = saturate( dot( N, V ) );
	vec2 uv = vec2( roughness, sqrt( 1.0 - dotNV ) );
	uv = uv * LUT_SCALE + LUT_BIAS;
	return uv;
}
float LTC_ClippedSphereFormFactor( const in vec3 f ) {
	float l = length( f );
	return max( ( l * l + f.z ) / ( l + 1.0 ), 0.0 );
}
vec3 LTC_EdgeVectorFormFactor( const in vec3 v1, const in vec3 v2 ) {
	float x = dot( v1, v2 );
	float y = abs( x );
	float a = 0.8543985 + ( 0.4965155 + 0.0145206 * y ) * y;
	float b = 3.4175940 + ( 4.1616724 + y ) * y;
	float v = a / b;
	float theta_sintheta = ( x > 0.0 ) ? v : 0.5 * inversesqrt( max( 1.0 - x * x, 1e-7 ) ) - v;
	return cross( v1, v2 ) * theta_sintheta;
}
vec3 LTC_Evaluate( const in vec3 N, const in vec3 V, const in vec3 P, const in mat3 mInv, const in vec3 rectCoords[ 4 ] ) {
	vec3 v1 = rectCoords[ 1 ] - rectCoords[ 0 ];
	vec3 v2 = rectCoords[ 3 ] - rectCoords[ 0 ];
	vec3 lightNormal = cross( v1, v2 );
	if( dot( lightNormal, P - rectCoords[ 0 ] ) < 0.0 ) return vec3( 0.0 );
	vec3 T1, T2;
	T1 = normalize( V - N * dot( V, N ) );
	T2 = - cross( N, T1 );
	mat3 mat = mInv * transpose( mat3( T1, T2, N ) );
	vec3 coords[ 4 ];
	coords[ 0 ] = mat * ( rectCoords[ 0 ] - P );
	coords[ 1 ] = mat * ( rectCoords[ 1 ] - P );
	coords[ 2 ] = mat * ( rectCoords[ 2 ] - P );
	coords[ 3 ] = mat * ( rectCoords[ 3 ] - P );
	coords[ 0 ] = normalize( coords[ 0 ] );
	coords[ 1 ] = normalize( coords[ 1 ] );
	coords[ 2 ] = normalize( coords[ 2 ] );
	coords[ 3 ] = normalize( coords[ 3 ] );
	vec3 vectorFormFactor = vec3( 0.0 );
	vectorFormFactor += LTC_EdgeVectorFormFactor( coords[ 0 ], coords[ 1 ] );
	vectorFormFactor += LTC_EdgeVectorFormFactor( coords[ 1 ], coords[ 2 ] );
	vectorFormFactor += LTC_EdgeVectorFormFactor( coords[ 2 ], coords[ 3 ] );
	vectorFormFactor += LTC_EdgeVectorFormFactor( coords[ 3 ], coords[ 0 ] );
	float result = LTC_ClippedSphereFormFactor( vectorFormFactor );
	return vec3( result );
}
#if defined( USE_SHEEN )
float D_Charlie( float roughness, float dotNH ) {
	float alpha = pow2( roughness );
	float invAlpha = 1.0 / alpha;
	float cos2h = dotNH * dotNH;
	float sin2h = max( 1.0 - cos2h, 0.0078125 );
	return ( 2.0 + invAlpha ) * pow( sin2h, invAlpha * 0.5 ) / ( 2.0 * PI );
}
float V_Neubelt( float dotNV, float dotNL ) {
	return saturate( 1.0 / ( 4.0 * ( dotNL + dotNV - dotNL * dotNV ) ) );
}
vec3 BRDF_Sheen( const in vec3 lightDir, const in vec3 viewDir, const in vec3 normal, vec3 sheenColor, const in float sheenRoughness ) {
	vec3 halfDir = normalize( lightDir + viewDir );
	float dotNL = saturate( dot( normal, lightDir ) );
	float dotNV = saturate( dot( normal, viewDir ) );
	float dotNH = saturate( dot( normal, halfDir ) );
	float D = D_Charlie( sheenRoughness, dotNH );
	float V = V_Neubelt( dotNV, dotNL );
	return sheenColor * ( D * V );
}
#endif
float IBLSheenBRDF( const in vec3 normal, const in vec3 viewDir, const in float roughness ) {
	float dotNV = saturate( dot( normal, viewDir ) );
	float r2 = roughness * roughness;
	float rInv = 1.0 / ( roughness + 0.1 );
	float a = -1.9362 + 1.0678 * roughness + 0.4573 * r2 - 0.8469 * rInv;
	float b = -0.6014 + 0.5538 * roughness - 0.4670 * r2 - 0.1255 * rInv;
	float DG = exp( a * dotNV + b );
	return saturate( DG );
}
vec3 EnvironmentBRDF( const in vec3 normal, const in vec3 viewDir, const in vec3 specularColor, const in float specularF90, const in float roughness ) {
	float dotNV = saturate( dot( normal, viewDir ) );
	vec2 fab = texture2D( dfgLUT, vec2( roughness, dotNV ) ).rg;
	return specularColor * fab.x + specularF90 * fab.y;
}
#ifdef USE_IRIDESCENCE
void computeMultiscatteringIridescence( const in vec2 fab, const in vec3 specularColor, const in float specularF90, const in float iridescence, const in vec3 iridescenceF0, inout vec3 singleScatter, inout vec3 multiScatter ) {
#else
void computeMultiscattering( const in vec2 fab, const in vec3 specularColor, const in float specularF90, inout vec3 singleScatter, inout vec3 multiScatter ) {
#endif
	#ifdef USE_IRIDESCENCE
		vec3 Fr = mix( specularColor, iridescenceF0, iridescence );
	#else
		vec3 Fr = specularColor;
	#endif
	vec3 FssEss = Fr * fab.x + specularF90 * fab.y;
	float Ess = fab.x + fab.y;
	float Ems = 1.0 - Ess;
	vec3 Favg = Fr + ( 1.0 - Fr ) * 0.047619;	vec3 Fms = FssEss * Favg / ( 1.0 - Ems * Favg );
	singleScatter += FssEss;
	multiScatter += Fms * Ems;
}
#if NUM_RECT_AREA_LIGHTS > 0
	void RE_Direct_RectArea_Physical( const in RectAreaLight rectAreaLight, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in PhysicalMaterial material, inout ReflectedLight reflectedLight ) {
		vec3 normal = geometryNormal;
		vec3 viewDir = geometryViewDir;
		vec3 position = geometryPosition;
		vec3 lightPos = rectAreaLight.position;
		vec3 halfWidth = rectAreaLight.halfWidth;
		vec3 halfHeight = rectAreaLight.halfHeight;
		vec3 lightColor = rectAreaLight.color;
		float roughness = material.roughness;
		vec3 rectCoords[ 4 ];
		rectCoords[ 0 ] = lightPos + halfWidth - halfHeight;		rectCoords[ 1 ] = lightPos - halfWidth - halfHeight;
		rectCoords[ 2 ] = lightPos - halfWidth + halfHeight;
		rectCoords[ 3 ] = lightPos + halfWidth + halfHeight;
		vec2 uv = LTC_Uv( normal, viewDir, roughness );
		vec4 t1 = texture2D( ltc_1, uv );
		vec4 t2 = texture2D( ltc_2, uv );
		mat3 mInv = mat3(
			vec3( t1.x, 0, t1.y ),
			vec3(    0, 1,    0 ),
			vec3( t1.z, 0, t1.w )
		);
		vec3 fresnel = ( material.specularColorBlended * t2.x + ( material.specularF90 - material.specularColorBlended ) * t2.y );
		reflectedLight.directSpecular += lightColor * fresnel * LTC_Evaluate( normal, viewDir, position, mInv, rectCoords );
		reflectedLight.directDiffuse += lightColor * material.diffuseContribution * LTC_Evaluate( normal, viewDir, position, mat3( 1.0 ), rectCoords );
		#ifdef USE_CLEARCOAT
			vec3 Ncc = geometryClearcoatNormal;
			vec2 uvClearcoat = LTC_Uv( Ncc, viewDir, material.clearcoatRoughness );
			vec4 t1Clearcoat = texture2D( ltc_1, uvClearcoat );
			vec4 t2Clearcoat = texture2D( ltc_2, uvClearcoat );
			mat3 mInvClearcoat = mat3(
				vec3( t1Clearcoat.x, 0, t1Clearcoat.y ),
				vec3(             0, 1,             0 ),
				vec3( t1Clearcoat.z, 0, t1Clearcoat.w )
			);
			vec3 fresnelClearcoat = material.clearcoatF0 * t2Clearcoat.x + ( material.clearcoatF90 - material.clearcoatF0 ) * t2Clearcoat.y;
			clearcoatSpecularDirect += lightColor * fresnelClearcoat * LTC_Evaluate( Ncc, viewDir, position, mInvClearcoat, rectCoords );
		#endif
	}
#endif
void RE_Direct_Physical( const in IncidentLight directLight, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in PhysicalMaterial material, inout ReflectedLight reflectedLight ) {
	float dotNL = saturate( dot( geometryNormal, directLight.direction ) );
	vec3 irradiance = dotNL * directLight.color;
	#ifdef USE_CLEARCOAT
		float dotNLcc = saturate( dot( geometryClearcoatNormal, directLight.direction ) );
		vec3 ccIrradiance = dotNLcc * directLight.color;
		clearcoatSpecularDirect += ccIrradiance * BRDF_GGX_Clearcoat( directLight.direction, geometryViewDir, geometryClearcoatNormal, material );
	#endif
	#ifdef USE_SHEEN
 
 		sheenSpecularDirect += irradiance * BRDF_Sheen( directLight.direction, geometryViewDir, geometryNormal, material.sheenColor, material.sheenRoughness );
 
 		float sheenAlbedoV = IBLSheenBRDF( geometryNormal, geometryViewDir, material.sheenRoughness );
 		float sheenAlbedoL = IBLSheenBRDF( geometryNormal, directLight.direction, material.sheenRoughness );
 
 		float sheenEnergyComp = 1.0 - max3( material.sheenColor ) * max( sheenAlbedoV, sheenAlbedoL );
 
 		irradiance *= sheenEnergyComp;
 
 	#endif
	vec3 specularBRDF = BRDF_GGX( directLight.direction, geometryViewDir, geometryNormal, material );
	#ifdef USE_RETROREFLECTION
		vec3 retroViewDir = reflect( - geometryViewDir, geometryNormal );
		vec3 retroSpecularBRDF = BRDF_GGX( directLight.direction, retroViewDir, geometryNormal, material );
		specularBRDF = mix( specularBRDF, retroSpecularBRDF, saturate( material.retroreflectivity ) );
	#endif
	reflectedLight.directSpecular += irradiance * specularBRDF * material.multiScatteringCompensation;
	vec3 halfDir = normalize( directLight.direction + geometryViewDir );
	float dotVH = saturate( dot( geometryViewDir, halfDir ) );
	vec3 F = F_Schlick( material.specularColor, material.specularF90, dotVH );
	#ifdef USE_RETROREFLECTION
		vec3 retroHalfDir = normalize( directLight.direction + retroViewDir );
		float dotRetroVH = saturate( dot( retroViewDir, retroHalfDir ) );
		vec3 retroF = F_Schlick( material.specularColor, material.specularF90, dotRetroVH );
		F = mix( F, retroF, saturate( material.retroreflectivity ) );
	#endif
	reflectedLight.directDiffuse += irradiance * BRDF_Lambert( material.diffuseContribution ) * ( 1.0 - F );
}
void RE_IndirectDiffuse_Physical( const in vec3 irradiance, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in PhysicalMaterial material, inout ReflectedLight reflectedLight ) {
	vec3 singleScattering = vec3( 0.0 );
	vec3 multiScattering = vec3( 0.0 );
	#ifdef USE_IRIDESCENCE
		computeMultiscatteringIridescence( material.dfg, material.specularColor, material.specularF90, material.iridescence, material.iridescenceF0Dielectric, singleScattering, multiScattering );
	#else
		computeMultiscattering( material.dfg, material.specularColor, material.specularF90, singleScattering, multiScattering );
	#endif
	vec3 diffuse = irradiance * BRDF_Lambert( material.diffuseContribution ) * ( 1.0 - singleScattering - multiScattering );
	#ifdef USE_SHEEN
		float sheenAlbedo = IBLSheenBRDF( geometryNormal, geometryViewDir, material.sheenRoughness );
		sheenSpecularIndirect += irradiance * material.sheenColor * sheenAlbedo * RECIPROCAL_PI;
		float sheenEnergyComp = 1.0 - max3( material.sheenColor ) * sheenAlbedo;
		diffuse *= sheenEnergyComp;
	#endif
	reflectedLight.indirectDiffuse += diffuse;
}
void RE_IndirectSpecular_Physical( const in vec3 radiance, const in vec3 irradiance, const in vec3 clearcoatRadiance, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in PhysicalMaterial material, inout ReflectedLight reflectedLight) {
	#ifdef USE_CLEARCOAT
		clearcoatSpecularIndirect += clearcoatRadiance * EnvironmentBRDF( geometryClearcoatNormal, geometryViewDir, material.clearcoatF0, material.clearcoatF90, material.clearcoatRoughness );
	#endif
	#ifdef USE_SHEEN
		sheenSpecularIndirect += irradiance * material.sheenColor * IBLSheenBRDF( geometryNormal, geometryViewDir, material.sheenRoughness ) * RECIPROCAL_PI;
 	#endif
	vec3 singleScatteringDielectric = vec3( 0.0 );
	vec3 multiScatteringDielectric = vec3( 0.0 );
	vec3 singleScatteringMetallic = vec3( 0.0 );
	vec3 multiScatteringMetallic = vec3( 0.0 );
	#ifdef USE_IRIDESCENCE
		computeMultiscatteringIridescence( material.dfg, material.specularColor, material.specularF90, material.iridescence, material.iridescenceF0Dielectric, singleScatteringDielectric, multiScatteringDielectric );
		computeMultiscatteringIridescence( material.dfg, material.diffuseColor, material.specularF90, material.iridescence, material.iridescenceF0Metallic, singleScatteringMetallic, multiScatteringMetallic );
	#else
		computeMultiscattering( material.dfg, material.specularColor, material.specularF90, singleScatteringDielectric, multiScatteringDielectric );
		computeMultiscattering( material.dfg, material.diffuseColor, material.specularF90, singleScatteringMetallic, multiScatteringMetallic );
	#endif
	vec3 singleScattering = mix( singleScatteringDielectric, singleScatteringMetallic, material.metalness );
	vec3 multiScattering = mix( multiScatteringDielectric, multiScatteringMetallic, material.metalness );
	vec3 totalScatteringDielectric = singleScatteringDielectric + multiScatteringDielectric;
	vec3 diffuse = material.diffuseContribution * ( 1.0 - totalScatteringDielectric );
	vec3 cosineWeightedIrradiance = irradiance * RECIPROCAL_PI;
	vec3 indirectSpecular = radiance * singleScattering;
	indirectSpecular += multiScattering * cosineWeightedIrradiance;
	vec3 indirectDiffuse = diffuse * cosineWeightedIrradiance;
	#ifdef USE_SHEEN
		float sheenAlbedo = IBLSheenBRDF( geometryNormal, geometryViewDir, material.sheenRoughness );
		float sheenEnergyComp = 1.0 - max3( material.sheenColor ) * sheenAlbedo;
		indirectSpecular *= sheenEnergyComp;
		indirectDiffuse *= sheenEnergyComp;
	#endif
	reflectedLight.indirectSpecular += indirectSpecular;
	reflectedLight.indirectDiffuse += indirectDiffuse;
}
#define RE_Direct				RE_Direct_Physical
#define RE_Direct_RectArea		RE_Direct_RectArea_Physical
#define RE_IndirectDiffuse		RE_IndirectDiffuse_Physical
#define RE_IndirectSpecular		RE_IndirectSpecular_Physical
float computeSpecularOcclusion( const in float dotNV, const in float ambientOcclusion, const in float roughness ) {
	return saturate( pow( dotNV + ambientOcclusion, exp2( - 16.0 * roughness - 1.0 ) ) - 1.0 + ambientOcclusion );
}`, Ig = `
vec3 geometryPosition = - vViewPosition;
vec3 geometryNormal = normal;
vec3 geometryViewDir = ( isOrthographic ) ? vec3( 0, 0, 1 ) : normalize( vViewPosition );
vec3 geometryClearcoatNormal = vec3( 0.0 );
#ifdef USE_CLEARCOAT
	geometryClearcoatNormal = clearcoatNormal;
#endif
#ifdef USE_IRIDESCENCE
	float dotNVi = saturate( dot( normal, geometryViewDir ) );
	if ( material.iridescenceThickness == 0.0 ) {
		material.iridescence = 0.0;
	} else {
		material.iridescence = saturate( material.iridescence );
	}
	if ( material.iridescence > 0.0 ) {
		vec3 iridescenceFresnelDielectric = evalIridescence( 1.0, material.iridescenceIOR, dotNVi, material.iridescenceThickness, material.specularColor );
		vec3 iridescenceFresnelMetallic = evalIridescence( 1.0, material.iridescenceIOR, dotNVi, material.iridescenceThickness, material.diffuseColor );
		material.iridescenceFresnel = mix( iridescenceFresnelDielectric, iridescenceFresnelMetallic, material.metalness );
		material.iridescenceF0Dielectric = Schlick_to_F0( iridescenceFresnelDielectric, 1.0, dotNVi );
		material.iridescenceF0Metallic = Schlick_to_F0( iridescenceFresnelMetallic, 1.0, dotNVi );
	}
#endif
#ifdef STANDARD
	float dotNVms = saturate( dot( geometryNormal, geometryViewDir ) );
	material.dfg = texture2D( dfgLUT, vec2( material.roughness, dotNVms ) ).rg;
	#if ( NUM_SUN_LIGHTS > 0 || NUM_DIR_LIGHTS > 0 || NUM_POINT_LIGHTS > 0 || NUM_SPOT_LIGHTS > 0 )
		float EssMs = material.dfg.x + material.dfg.y;
		material.multiScatteringCompensation = 1.0 + material.specularColorBlended * ( 1.0 / EssMs - 1.0 );
	#endif
#endif
IncidentLight directLight;
#if ( NUM_POINT_LIGHTS > 0 ) && defined( RE_Direct )
	PointLight pointLight;
	#if defined( USE_SHADOWMAP ) && NUM_POINT_LIGHT_SHADOWS > 0
	PointLightShadow pointLightShadow;
	#endif
	#pragma unroll_loop_start
	for ( int i = 0; i < NUM_POINT_LIGHTS; i ++ ) {
		pointLight = pointLights[ i ];
		getPointLightInfo( pointLight, geometryPosition, directLight );
		#if defined( USE_SHADOWMAP ) && ( UNROLLED_LOOP_INDEX < NUM_POINT_LIGHT_SHADOWS ) && ( defined( SHADOWMAP_TYPE_PCF ) || defined( SHADOWMAP_TYPE_BASIC ) )
		pointLightShadow = pointLightShadows[ i ];
		directLight.color *= ( directLight.visible && receiveShadow ) ? getPointShadow( pointShadowMap[ i ], pointLightShadow.shadowMapSize, pointLightShadow.shadowIntensity, pointLightShadow.shadowBias, pointLightShadow.shadowRadius, vPointShadowCoord[ i ], pointLightShadow.shadowCameraNear, pointLightShadow.shadowCameraFar ) : 1.0;
		#endif
		RE_Direct( directLight, geometryPosition, geometryNormal, geometryViewDir, geometryClearcoatNormal, material, reflectedLight );
	}
	#pragma unroll_loop_end
#endif
#if ( NUM_SPOT_LIGHTS > 0 ) && defined( RE_Direct )
	SpotLight spotLight;
	vec4 spotColor;
	vec3 spotLightCoord;
	bool inSpotLightMap;
	#if defined( USE_SHADOWMAP ) && NUM_SPOT_LIGHT_SHADOWS > 0
	SpotLightShadow spotLightShadow;
	#endif
	#pragma unroll_loop_start
	for ( int i = 0; i < NUM_SPOT_LIGHTS; i ++ ) {
		spotLight = spotLights[ i ];
		getSpotLightInfo( spotLight, geometryPosition, directLight );
		#if ( UNROLLED_LOOP_INDEX < NUM_SPOT_LIGHT_SHADOWS_WITH_MAPS )
		#define SPOT_LIGHT_MAP_INDEX UNROLLED_LOOP_INDEX
		#elif ( UNROLLED_LOOP_INDEX < NUM_SPOT_LIGHT_SHADOWS )
		#define SPOT_LIGHT_MAP_INDEX NUM_SPOT_LIGHT_MAPS
		#else
		#define SPOT_LIGHT_MAP_INDEX ( UNROLLED_LOOP_INDEX - NUM_SPOT_LIGHT_SHADOWS + NUM_SPOT_LIGHT_SHADOWS_WITH_MAPS )
		#endif
		#if ( SPOT_LIGHT_MAP_INDEX < NUM_SPOT_LIGHT_MAPS )
			spotLightCoord = vSpotLightCoord[ i ].xyz / vSpotLightCoord[ i ].w;
			inSpotLightMap = all( lessThan( abs( spotLightCoord * 2. - 1. ), vec3( 1.0 ) ) );
			spotColor = texture2D( spotLightMap[ SPOT_LIGHT_MAP_INDEX ], spotLightCoord.xy );
			directLight.color = inSpotLightMap ? directLight.color * spotColor.rgb : directLight.color;
		#endif
		#undef SPOT_LIGHT_MAP_INDEX
		#if defined( USE_SHADOWMAP ) && ( UNROLLED_LOOP_INDEX < NUM_SPOT_LIGHT_SHADOWS )
		spotLightShadow = spotLightShadows[ i ];
		directLight.color *= ( directLight.visible && receiveShadow ) ? getShadow( spotShadowMap[ i ], spotLightShadow.shadowMapSize, spotLightShadow.shadowIntensity, spotLightShadow.shadowBias, spotLightShadow.shadowRadius, vSpotLightCoord[ i ] ) : 1.0;
		#endif
		RE_Direct( directLight, geometryPosition, geometryNormal, geometryViewDir, geometryClearcoatNormal, material, reflectedLight );
	}
	#pragma unroll_loop_end
#endif
#if ( NUM_SUN_LIGHTS > 0 ) && defined( RE_Direct )
	SunLight sunLight;
	#if defined( USE_SHADOWMAP ) && NUM_SUN_LIGHT_SHADOWS > 0
	SunLightShadow sunLightShadow;
	#endif
	#pragma unroll_loop_start
	for ( int i = 0; i < NUM_SUN_LIGHTS; i ++ ) {
		sunLight = sunLights[ i ];
		getSunLightInfo( sunLight, directLight );
		#if defined( USE_SHADOWMAP ) && ( UNROLLED_LOOP_INDEX < NUM_SUN_LIGHT_SHADOWS )
		sunLightShadow = sunLightShadows[ i ];
		directLight.color *= ( directLight.visible && receiveShadow ) ? getSunShadow( sunShadowMap[ i ], sunLightShadow, UNROLLED_LOOP_INDEX ) : 1.0;
		#endif
		RE_Direct( directLight, geometryPosition, geometryNormal, geometryViewDir, geometryClearcoatNormal, material, reflectedLight );
	}
	#pragma unroll_loop_end
#endif
#if ( NUM_DIR_LIGHTS > 0 ) && defined( RE_Direct )
	DirectionalLight directionalLight;
	#if defined( USE_SHADOWMAP ) && NUM_DIR_LIGHT_SHADOWS > 0
	DirectionalLightShadow directionalLightShadow;
	#endif
	#pragma unroll_loop_start
	for ( int i = 0; i < NUM_DIR_LIGHTS; i ++ ) {
		directionalLight = directionalLights[ i ];
		getDirectionalLightInfo( directionalLight, directLight );
		#if defined( USE_SHADOWMAP ) && ( UNROLLED_LOOP_INDEX < NUM_DIR_LIGHT_SHADOWS )
		directionalLightShadow = directionalLightShadows[ i ];
		directLight.color *= ( directLight.visible && receiveShadow ) ? getShadow( directionalShadowMap[ i ], directionalLightShadow.shadowMapSize, directionalLightShadow.shadowIntensity, directionalLightShadow.shadowBias, directionalLightShadow.shadowRadius, vDirectionalShadowCoord[ i ] ) : 1.0;
		#endif
		RE_Direct( directLight, geometryPosition, geometryNormal, geometryViewDir, geometryClearcoatNormal, material, reflectedLight );
	}
	#pragma unroll_loop_end
#endif
#if ( NUM_RECT_AREA_LIGHTS > 0 ) && defined( RE_Direct_RectArea )
	RectAreaLight rectAreaLight;
	#pragma unroll_loop_start
	for ( int i = 0; i < NUM_RECT_AREA_LIGHTS; i ++ ) {
		rectAreaLight = rectAreaLights[ i ];
		RE_Direct_RectArea( rectAreaLight, geometryPosition, geometryNormal, geometryViewDir, geometryClearcoatNormal, material, reflectedLight );
	}
	#pragma unroll_loop_end
#endif
#if defined( RE_IndirectDiffuse )
	vec3 iblIrradiance = vec3( 0.0 );
	vec3 irradiance = getAmbientLightIrradiance( ambientLightColor );
	#if defined( USE_LIGHT_PROBES )
		irradiance += getLightProbeIrradiance( lightProbe, geometryNormal );
	#endif
	#if ( NUM_HEMI_LIGHTS > 0 )
		#pragma unroll_loop_start
		for ( int i = 0; i < NUM_HEMI_LIGHTS; i ++ ) {
			irradiance += getHemisphereLightIrradiance( hemisphereLights[ i ], geometryNormal );
		}
		#pragma unroll_loop_end
	#endif
	#ifdef USE_LIGHT_PROBES_GRID
		vec3 probeWorldPos = ( ( vec4( geometryPosition, 1.0 ) - viewMatrix[ 3 ] ) * viewMatrix ).xyz;
		vec3 probeWorldNormal = transformNormalByInverseViewMatrix( geometryNormal, viewMatrix );
		irradiance += getLightProbeGridIrradiance( probeWorldPos, probeWorldNormal );
	#endif
#endif
#if defined( RE_IndirectSpecular )
	vec3 radiance = vec3( 0.0 );
	vec3 clearcoatRadiance = vec3( 0.0 );
#endif`, Ng = `#if defined( RE_IndirectDiffuse )
	#ifdef USE_LIGHTMAP
		vec4 lightMapTexel = texture2D( lightMap, vLightMapUv );
		vec3 lightMapIrradiance = lightMapTexel.rgb * lightMapIntensity;
		irradiance += lightMapIrradiance;
	#endif
	#if defined( USE_ENVMAP ) && defined( ENVMAP_TYPE_CUBE_UV )
		#if defined( STANDARD ) || defined( LAMBERT ) || defined( PHONG )
			iblIrradiance += getIBLIrradiance( geometryNormal );
		#endif
	#endif
#endif
#if defined( USE_ENVMAP ) && defined( RE_IndirectSpecular )
	#ifdef USE_ANISOTROPY
		vec3 iblRadiance = getIBLAnisotropyRadiance( geometryViewDir, geometryNormal, material.roughness, material.anisotropyB, material.anisotropy );
	#else
		vec3 iblRadiance = getIBLRadiance( geometryViewDir, geometryNormal, material.roughness );
	#endif
	#ifdef USE_RETROREFLECTION
		#ifdef USE_ANISOTROPY
			vec3 retroIBLRadiance = getIBLAnisotropyRetroRadiance( geometryViewDir, geometryNormal, material.roughness, material.anisotropyB, material.anisotropy );
		#else
			vec3 retroIBLRadiance = getIBLRetroRadiance( geometryViewDir, geometryNormal, material.roughness );
		#endif
		iblRadiance = mix( iblRadiance, retroIBLRadiance, saturate( material.retroreflectivity ) );
	#endif
	radiance += iblRadiance;
	#ifdef USE_CLEARCOAT
		clearcoatRadiance += getIBLRadiance( geometryViewDir, geometryClearcoatNormal, material.clearcoatRoughness );
	#endif
#endif`, Ug = `#if defined( RE_IndirectDiffuse )
	#if defined( LAMBERT ) || defined( PHONG )
		irradiance += iblIrradiance;
	#endif
	RE_IndirectDiffuse( irradiance, geometryPosition, geometryNormal, geometryViewDir, geometryClearcoatNormal, material, reflectedLight );
#endif
#if defined( RE_IndirectSpecular )
	RE_IndirectSpecular( radiance, iblIrradiance, clearcoatRadiance, geometryPosition, geometryNormal, geometryViewDir, geometryClearcoatNormal, material, reflectedLight );
#endif`, Dg = `#ifdef USE_LIGHT_PROBES_GRID
uniform highp sampler3D probesSH;
uniform vec3 probesMin;
uniform vec3 probesMax;
uniform vec3 probesResolution;
vec3 getLightProbeGridIrradiance( vec3 worldPos, vec3 worldNormal ) {
	vec3 res = probesResolution;
	vec3 gridRange = probesMax - probesMin;
	vec3 resMinusOne = res - 1.0;
	vec3 probeSpacing = gridRange / resMinusOne;
	vec3 samplePos = worldPos + worldNormal * probeSpacing * 0.5;
	vec3 uvw = clamp( ( samplePos - probesMin ) / gridRange, 0.0, 1.0 );
	uvw = uvw * resMinusOne / res + 0.5 / res;
	float nz          = res.z;
	float paddedSlices = nz + 2.0;
	float atlasDepth  = 7.0 * paddedSlices;
	float uvZBase     = uvw.z * nz + 1.0;
	vec4 s0 = texture( probesSH, vec3( uvw.xy, ( uvZBase                       ) / atlasDepth ) );
	vec4 s1 = texture( probesSH, vec3( uvw.xy, ( uvZBase +       paddedSlices   ) / atlasDepth ) );
	vec4 s2 = texture( probesSH, vec3( uvw.xy, ( uvZBase + 2.0 * paddedSlices   ) / atlasDepth ) );
	vec4 s3 = texture( probesSH, vec3( uvw.xy, ( uvZBase + 3.0 * paddedSlices   ) / atlasDepth ) );
	vec4 s4 = texture( probesSH, vec3( uvw.xy, ( uvZBase + 4.0 * paddedSlices   ) / atlasDepth ) );
	vec4 s5 = texture( probesSH, vec3( uvw.xy, ( uvZBase + 5.0 * paddedSlices   ) / atlasDepth ) );
	vec4 s6 = texture( probesSH, vec3( uvw.xy, ( uvZBase + 6.0 * paddedSlices   ) / atlasDepth ) );
	vec3 c0 = s0.xyz;
	vec3 c1 = vec3( s0.w, s1.xy );
	vec3 c2 = vec3( s1.zw, s2.x );
	vec3 c3 = s2.yzw;
	vec3 c4 = s3.xyz;
	vec3 c5 = vec3( s3.w, s4.xy );
	vec3 c6 = vec3( s4.zw, s5.x );
	vec3 c7 = s5.yzw;
	vec3 c8 = s6.xyz;
	float x = worldNormal.x, y = worldNormal.y, z = worldNormal.z;
	vec3 result = c0 * 0.886227;
	result += c1 * 2.0 * 0.511664 * y;
	result += c2 * 2.0 * 0.511664 * z;
	result += c3 * 2.0 * 0.511664 * x;
	result += c4 * 2.0 * 0.429043 * x * y;
	result += c5 * 2.0 * 0.429043 * y * z;
	result += c6 * ( 0.743125 * z * z - 0.247708 );
	result += c7 * 2.0 * 0.429043 * x * z;
	result += c8 * 0.429043 * ( x * x - y * y );
	return max( result, vec3( 0.0 ) );
}
#endif`, Og = `#if defined( USE_LOGARITHMIC_DEPTH_BUFFER )
	gl_FragDepth = vIsPerspective == 0.0 ? gl_FragCoord.z : log2( vFragDepth ) * logDepthBufFC * 0.5;
#endif`, Fg = `#if defined( USE_LOGARITHMIC_DEPTH_BUFFER )
	uniform float logDepthBufFC;
	varying float vFragDepth;
	varying float vIsPerspective;
#endif`, Bg = `#ifdef USE_LOGARITHMIC_DEPTH_BUFFER
	varying float vFragDepth;
	varying float vIsPerspective;
#endif`, Vg = `#ifdef USE_LOGARITHMIC_DEPTH_BUFFER
	vFragDepth = 1.0 + gl_Position.w;
	vIsPerspective = float( isPerspectiveMatrix( projectionMatrix ) );
#endif`, Hg = `#ifdef USE_MAP
	vec4 sampledDiffuseColor = texture2D( map, vMapUv );
	#ifdef DECODE_VIDEO_TEXTURE
		sampledDiffuseColor = sRGBTransferEOTF( sampledDiffuseColor );
	#endif
	diffuseColor *= sampledDiffuseColor;
#endif`, kg = `#ifdef USE_MAP
	uniform sampler2D map;
#endif`, zg = `#if defined( USE_MAP ) || defined( USE_ALPHAMAP )
	#if defined( USE_POINTS_UV )
		vec2 uv = vUv;
	#else
		vec2 uv = ( uvTransform * vec3( gl_PointCoord.x, 1.0 - gl_PointCoord.y, 1 ) ).xy;
	#endif
#endif
#ifdef USE_MAP
	diffuseColor *= texture2D( map, uv );
#endif
#ifdef USE_ALPHAMAP
	diffuseColor.a *= texture2D( alphaMap, uv ).g;
#endif`, Gg = `#if defined( USE_POINTS_UV )
	varying vec2 vUv;
#else
	#if defined( USE_MAP ) || defined( USE_ALPHAMAP )
		uniform mat3 uvTransform;
	#endif
#endif
#ifdef USE_MAP
	uniform sampler2D map;
#endif
#ifdef USE_ALPHAMAP
	uniform sampler2D alphaMap;
#endif`, Wg = `float metalnessFactor = metalness;
#ifdef USE_METALNESSMAP
	vec4 texelMetalness = texture2D( metalnessMap, vMetalnessMapUv );
	metalnessFactor *= texelMetalness.b;
#endif`, Xg = `#ifdef USE_METALNESSMAP
	uniform sampler2D metalnessMap;
#endif`, jg = `#ifdef USE_INSTANCING_MORPH
	float morphTargetInfluences[ MORPHTARGETS_COUNT ];
	float morphTargetBaseInfluence = texelFetch( morphTexture, ivec2( 0, gl_InstanceID ), 0 ).r;
	for ( int i = 0; i < MORPHTARGETS_COUNT; i ++ ) {
		morphTargetInfluences[i] =  texelFetch( morphTexture, ivec2( i + 1, gl_InstanceID ), 0 ).r;
	}
#endif`, qg = `#if defined( USE_MORPHCOLORS )
	vColor *= morphTargetBaseInfluence;
	for ( int i = 0; i < MORPHTARGETS_COUNT; i ++ ) {
		#if defined( USE_COLOR_ALPHA )
			if ( morphTargetInfluences[ i ] != 0.0 ) vColor += getMorph( gl_VertexID, i, 2 ) * morphTargetInfluences[ i ];
		#elif defined( USE_COLOR )
			if ( morphTargetInfluences[ i ] != 0.0 ) vColor += getMorph( gl_VertexID, i, 2 ).rgb * morphTargetInfluences[ i ];
		#endif
	}
#endif`, Yg = `#ifdef USE_MORPHNORMALS
	objectNormal *= morphTargetBaseInfluence;
	for ( int i = 0; i < MORPHTARGETS_COUNT; i ++ ) {
		if ( morphTargetInfluences[ i ] != 0.0 ) objectNormal += getMorph( gl_VertexID, i, 1 ).xyz * morphTargetInfluences[ i ];
	}
#endif`, Kg = `#ifdef USE_MORPHTARGETS
	#ifndef USE_INSTANCING_MORPH
		uniform float morphTargetBaseInfluence;
		uniform float morphTargetInfluences[ MORPHTARGETS_COUNT ];
	#endif
	uniform sampler2DArray morphTargetsTexture;
	uniform ivec2 morphTargetsTextureSize;
	vec4 getMorph( const in int vertexIndex, const in int morphTargetIndex, const in int offset ) {
		int texelIndex = vertexIndex * MORPHTARGETS_TEXTURE_STRIDE + offset;
		int y = texelIndex / morphTargetsTextureSize.x;
		int x = texelIndex - y * morphTargetsTextureSize.x;
		ivec3 morphUV = ivec3( x, y, morphTargetIndex );
		return texelFetch( morphTargetsTexture, morphUV, 0 );
	}
#endif`, Zg = `#ifdef USE_MORPHTARGETS
	transformed *= morphTargetBaseInfluence;
	for ( int i = 0; i < MORPHTARGETS_COUNT; i ++ ) {
		if ( morphTargetInfluences[ i ] != 0.0 ) transformed += getMorph( gl_VertexID, i, 0 ).xyz * morphTargetInfluences[ i ];
	}
#endif`, $g = `float faceDirection = gl_FrontFacing ? 1.0 : - 1.0;
#ifdef FLAT_SHADED
	vec3 fdx = dFdx( vViewPosition );
	vec3 fdy = dFdy( vViewPosition );
	vec3 normal = normalize( cross( fdx, fdy ) );
#else
	vec3 normal = normalize( vNormal );
	#ifdef DOUBLE_SIDED
		normal *= faceDirection;
	#endif
#endif
#if defined( USE_NORMALMAP_TANGENTSPACE ) || defined( USE_CLEARCOAT_NORMALMAP ) || defined( USE_ANISOTROPY )
	#ifdef USE_TANGENT
		mat3 tbn = mat3( normalize( vTangent ), normalize( vBitangent ), normal );
	#else
		mat3 tbn = getTangentFrame( - vViewPosition, normal,
		#if defined( USE_NORMALMAP )
			vNormalMapUv
		#elif defined( USE_CLEARCOAT_NORMALMAP )
			vClearcoatNormalMapUv
		#else
			vUv
		#endif
		);
	#endif
	#ifdef DOUBLE_SIDED
		tbn[0] *= faceDirection;
		tbn[1] *= faceDirection;
	#endif
#endif
#ifdef USE_CLEARCOAT_NORMALMAP
	#ifdef USE_TANGENT
		mat3 tbn2 = mat3( normalize( vTangent ), normalize( vBitangent ), normal );
	#else
		mat3 tbn2 = getTangentFrame( - vViewPosition, normal, vClearcoatNormalMapUv );
	#endif
	#ifdef DOUBLE_SIDED
		tbn2[0] *= faceDirection;
		tbn2[1] *= faceDirection;
	#endif
#endif
vec3 nonPerturbedNormal = normal;`, Qg = `#ifdef USE_NORMALMAP_OBJECTSPACE
	normal = texture2D( normalMap, vNormalMapUv ).xyz * 2.0 - 1.0;
	#ifdef FLIP_SIDED
		normal = - normal;
	#endif
	#ifdef DOUBLE_SIDED
		normal = normal * faceDirection;
	#endif
	normal = normalize( normalMatrix * normal );
#elif defined( USE_NORMALMAP_TANGENTSPACE )
	vec3 mapN = texture2D( normalMap, vNormalMapUv ).xyz * 2.0 - 1.0;
	#if defined( USE_PACKED_NORMALMAP )
		mapN = vec3( mapN.xy, sqrt( saturate( 1.0 - dot( mapN.xy, mapN.xy ) ) ) );
	#endif
	mapN.xy *= normalScale;
	normal = normalize( tbn * mapN );
#elif defined( USE_BUMPMAP )
	normal = perturbNormalArb( - vViewPosition, normal, dHdxy_fwd(), faceDirection );
#endif`, Jg = `#ifndef FLAT_SHADED
	varying vec3 vNormal;
	#ifdef USE_TANGENT
		varying vec3 vTangent;
		varying vec3 vBitangent;
	#endif
#endif`, e_ = `#ifndef FLAT_SHADED
	varying vec3 vNormal;
	#ifdef USE_TANGENT
		varying vec3 vTangent;
		varying vec3 vBitangent;
	#endif
#endif`, t_ = `#ifndef FLAT_SHADED
	vNormal = normalize( transformedNormal );
	#ifdef USE_TANGENT
		vTangent = normalize( transformedTangent );
		vBitangent = normalize( cross( vNormal, vTangent ) * tangent.w );
		#ifdef FLIP_SIDED
			vBitangent = - vBitangent;
		#endif
	#endif
#endif`, i_ = `#ifdef USE_NORMALMAP
	uniform sampler2D normalMap;
	uniform vec2 normalScale;
#endif
#ifdef USE_NORMALMAP_OBJECTSPACE
	uniform mat3 normalMatrix;
#endif
#if ! defined ( USE_TANGENT ) && ( defined ( USE_NORMALMAP_TANGENTSPACE ) || defined ( USE_CLEARCOAT_NORMALMAP ) || defined( USE_ANISOTROPY ) )
	mat3 getTangentFrame( vec3 eye_pos, vec3 surf_norm, vec2 uv ) {
		vec3 q0 = dFdx( eye_pos.xyz );
		vec3 q1 = dFdy( eye_pos.xyz );
		vec2 st0 = dFdx( uv.st );
		vec2 st1 = dFdy( uv.st );
		vec3 N = surf_norm;
		vec3 q1perp = cross( q1, N );
		vec3 q0perp = cross( N, q0 );
		vec3 T = q1perp * st0.x + q0perp * st1.x;
		vec3 B = q1perp * st0.y + q0perp * st1.y;
		float det = max( dot( T, T ), dot( B, B ) );
		float scale = ( det == 0.0 ) ? 0.0 : inversesqrt( det );
		return mat3( T * scale, B * scale, N );
	}
#endif`, r_ = `#ifdef USE_CLEARCOAT
	vec3 clearcoatNormal = nonPerturbedNormal;
#endif`, n_ = `#ifdef USE_CLEARCOAT_NORMALMAP
	vec3 clearcoatMapN = texture2D( clearcoatNormalMap, vClearcoatNormalMapUv ).xyz * 2.0 - 1.0;
	clearcoatMapN.xy *= clearcoatNormalScale;
	clearcoatNormal = normalize( tbn2 * clearcoatMapN );
#endif`, s_ = `#ifdef USE_CLEARCOATMAP
	uniform sampler2D clearcoatMap;
#endif
#ifdef USE_CLEARCOAT_NORMALMAP
	uniform sampler2D clearcoatNormalMap;
	uniform vec2 clearcoatNormalScale;
#endif
#ifdef USE_CLEARCOAT_ROUGHNESSMAP
	uniform sampler2D clearcoatRoughnessMap;
#endif`, a_ = `#ifdef USE_IRIDESCENCEMAP
	uniform sampler2D iridescenceMap;
#endif
#ifdef USE_IRIDESCENCE_THICKNESSMAP
	uniform sampler2D iridescenceThicknessMap;
#endif`, o_ = `#ifdef OPAQUE
diffuseColor.a = 1.0;
#endif
#ifdef USE_TRANSMISSION
diffuseColor.a *= material.transmissionAlpha;
#endif
gl_FragColor = vec4( outgoingLight, diffuseColor.a );`, l_ = `vec3 packNormalToRGB( const in vec3 normal ) {
	return normalize( normal ) * 0.5 + 0.5;
}
vec3 unpackRGBToNormal( const in vec3 rgb ) {
	return 2.0 * rgb.xyz - 1.0;
}
const float PackUpscale = 256. / 255.;const float UnpackDownscale = 255. / 256.;const float ShiftRight8 = 1. / 256.;
const float Inv255 = 1. / 255.;
const vec4 PackFactors = vec4( 1.0, 256.0, 256.0 * 256.0, 256.0 * 256.0 * 256.0 );
const vec2 UnpackFactors2 = vec2( UnpackDownscale, 1.0 / PackFactors.g );
const vec3 UnpackFactors3 = vec3( UnpackDownscale / PackFactors.rg, 1.0 / PackFactors.b );
const vec4 UnpackFactors4 = vec4( UnpackDownscale / PackFactors.rgb, 1.0 / PackFactors.a );
vec4 packDepthToRGBA( const in float v ) {
	if( v <= 0.0 )
		return vec4( 0., 0., 0., 0. );
	if( v >= 1.0 )
		return vec4( 1., 1., 1., 1. );
	float vuf;
	float af = modf( v * PackFactors.a, vuf );
	float bf = modf( vuf * ShiftRight8, vuf );
	float gf = modf( vuf * ShiftRight8, vuf );
	return vec4( vuf * Inv255, gf * PackUpscale, bf * PackUpscale, af );
}
vec3 packDepthToRGB( const in float v ) {
	if( v <= 0.0 )
		return vec3( 0., 0., 0. );
	if( v >= 1.0 )
		return vec3( 1., 1., 1. );
	float vuf;
	float bf = modf( v * PackFactors.b, vuf );
	float gf = modf( vuf * ShiftRight8, vuf );
	return vec3( vuf * Inv255, gf * PackUpscale, bf );
}
vec2 packDepthToRG( const in float v ) {
	if( v <= 0.0 )
		return vec2( 0., 0. );
	if( v >= 1.0 )
		return vec2( 1., 1. );
	float vuf;
	float gf = modf( v * 256., vuf );
	return vec2( vuf * Inv255, gf );
}
float unpackRGBAToDepth( const in vec4 v ) {
	return dot( v, UnpackFactors4 );
}
float unpackRGBToDepth( const in vec3 v ) {
	return dot( v, UnpackFactors3 );
}
float unpackRGToDepth( const in vec2 v ) {
	return v.r * UnpackFactors2.r + v.g * UnpackFactors2.g;
}
vec4 pack2HalfToRGBA( const in vec2 v ) {
	vec4 r = vec4( v.x, fract( v.x * 255.0 ), v.y, fract( v.y * 255.0 ) );
	return vec4( r.x - r.y / 255.0, r.y, r.z - r.w / 255.0, r.w );
}
vec2 unpackRGBATo2Half( const in vec4 v ) {
	return vec2( v.x + ( v.y / 255.0 ), v.z + ( v.w / 255.0 ) );
}
float viewZToOrthographicDepth( const in float viewZ, const in float near, const in float far ) {
	return ( viewZ + near ) / ( near - far );
}
float orthographicDepthToViewZ( const in float depth, const in float near, const in float far ) {
	#ifdef USE_REVERSED_DEPTH_BUFFER
	
		return depth * ( far - near ) - far;
	#else
		return depth * ( near - far ) - near;
	#endif
}
float viewZToPerspectiveDepth( const in float viewZ, const in float near, const in float far ) {
	return ( ( near + viewZ ) * far ) / ( ( far - near ) * viewZ );
}
float perspectiveDepthToViewZ( const in float depth, const in float near, const in float far ) {
	
	#ifdef USE_REVERSED_DEPTH_BUFFER
		return ( near * far ) / ( ( near - far ) * depth - near );
	#else
		return ( near * far ) / ( ( far - near ) * depth - far );
	#endif
}`, c_ = `#ifdef PREMULTIPLIED_ALPHA
	gl_FragColor.rgb *= gl_FragColor.a;
#endif`, h_ = `vec4 mvPosition = vec4( transformed, 1.0 );
#ifdef USE_BATCHING
	mvPosition = batchingMatrix * mvPosition;
#endif
#ifdef USE_INSTANCING
	mvPosition = instanceMatrix * mvPosition;
#endif
mvPosition = modelViewMatrix * mvPosition;
gl_Position = projectionMatrix * mvPosition;`, u_ = `#ifdef DITHERING
	gl_FragColor.rgb = dithering( gl_FragColor.rgb );
#endif`, d_ = `#ifdef DITHERING
	vec3 dithering( vec3 color ) {
		float grid_position = rand( gl_FragCoord.xy );
		vec3 dither_shift_RGB = vec3( 0.25 / 255.0, -0.25 / 255.0, 0.25 / 255.0 );
		dither_shift_RGB = mix( 2.0 * dither_shift_RGB, -2.0 * dither_shift_RGB, grid_position );
		return color + dither_shift_RGB;
	}
#endif`, p_ = `float roughnessFactor = roughness;
#ifdef USE_ROUGHNESSMAP
	vec4 texelRoughness = texture2D( roughnessMap, vRoughnessMapUv );
	roughnessFactor *= texelRoughness.g;
#endif`, f_ = `#ifdef USE_ROUGHNESSMAP
	uniform sampler2D roughnessMap;
#endif`, m_ = `#if NUM_SPOT_LIGHT_COORDS > 0
	varying vec4 vSpotLightCoord[ NUM_SPOT_LIGHT_COORDS ];
#endif
#if NUM_SPOT_LIGHT_MAPS > 0
	uniform sampler2D spotLightMap[ NUM_SPOT_LIGHT_MAPS ];
#endif
#ifdef USE_SHADOWMAP
	#if NUM_SUN_LIGHT_SHADOWS > 0
		#define SUN_LIGHT_CASCADES 2
		#if defined( SHADOWMAP_TYPE_PCF )
			uniform sampler2DShadow sunShadowMap[ NUM_SUN_LIGHT_SHADOWS ];
		#else
			uniform sampler2D sunShadowMap[ NUM_SUN_LIGHT_SHADOWS ];
		#endif
		uniform mat4 sunShadowMatrix[ NUM_SUN_LIGHT_SHADOWS * SUN_LIGHT_CASCADES ];
		uniform vec4 sunShadowCascade[ NUM_SUN_LIGHT_SHADOWS * SUN_LIGHT_CASCADES ];
		varying vec4 vSunShadowWorldPosition;
		varying vec3 vSunShadowWorldNormal;
		struct SunLightShadow {
			float shadowIntensity;
			float shadowBias;
			float shadowNormalBias;
			float shadowRadius;
			vec2 shadowMapSize;
		};
		uniform SunLightShadow sunLightShadows[ NUM_SUN_LIGHT_SHADOWS ];
	#endif
	#if NUM_DIR_LIGHT_SHADOWS > 0
		#if defined( SHADOWMAP_TYPE_PCF )
			uniform sampler2DShadow directionalShadowMap[ NUM_DIR_LIGHT_SHADOWS ];
		#else
			uniform sampler2D directionalShadowMap[ NUM_DIR_LIGHT_SHADOWS ];
		#endif
		varying vec4 vDirectionalShadowCoord[ NUM_DIR_LIGHT_SHADOWS ];
		struct DirectionalLightShadow {
			float shadowIntensity;
			float shadowBias;
			float shadowNormalBias;
			float shadowRadius;
			vec2 shadowMapSize;
		};
		uniform DirectionalLightShadow directionalLightShadows[ NUM_DIR_LIGHT_SHADOWS ];
	#endif
	#if NUM_SPOT_LIGHT_SHADOWS > 0
		#if defined( SHADOWMAP_TYPE_PCF )
			uniform sampler2DShadow spotShadowMap[ NUM_SPOT_LIGHT_SHADOWS ];
		#else
			uniform sampler2D spotShadowMap[ NUM_SPOT_LIGHT_SHADOWS ];
		#endif
		struct SpotLightShadow {
			float shadowIntensity;
			float shadowBias;
			float shadowNormalBias;
			float shadowRadius;
			vec2 shadowMapSize;
		};
		uniform SpotLightShadow spotLightShadows[ NUM_SPOT_LIGHT_SHADOWS ];
	#endif
	#if NUM_POINT_LIGHT_SHADOWS > 0
		#if defined( SHADOWMAP_TYPE_PCF )
			uniform samplerCubeShadow pointShadowMap[ NUM_POINT_LIGHT_SHADOWS ];
		#elif defined( SHADOWMAP_TYPE_BASIC )
			uniform samplerCube pointShadowMap[ NUM_POINT_LIGHT_SHADOWS ];
		#endif
		varying vec4 vPointShadowCoord[ NUM_POINT_LIGHT_SHADOWS ];
		struct PointLightShadow {
			float shadowIntensity;
			float shadowBias;
			float shadowNormalBias;
			float shadowRadius;
			vec2 shadowMapSize;
			float shadowCameraNear;
			float shadowCameraFar;
		};
		uniform PointLightShadow pointLightShadows[ NUM_POINT_LIGHT_SHADOWS ];
	#endif
	#if defined( SHADOWMAP_TYPE_PCF )
		float interleavedGradientNoise( vec2 position ) {
			return fract( 52.9829189 * fract( dot( position, vec2( 0.06711056, 0.00583715 ) ) ) );
		}
		vec2 vogelDiskSample( int sampleIndex, int samplesCount, float phi ) {
			const float goldenAngle = 2.399963229728653;
			float r = sqrt( ( float( sampleIndex ) + 0.5 ) / float( samplesCount ) );
			float theta = float( sampleIndex ) * goldenAngle + phi;
			return vec2( cos( theta ), sin( theta ) ) * r;
		}
	#endif
	#if defined( SHADOWMAP_TYPE_PCF )
		float getShadow( sampler2DShadow shadowMap, vec2 shadowMapSize, float shadowIntensity, float shadowBias, float shadowRadius, vec4 shadowCoord ) {
			float shadow = 1.0;
			shadowCoord.xyz /= shadowCoord.w;
			shadowCoord.z += shadowBias;
			bool inFrustum = shadowCoord.x >= 0.0 && shadowCoord.x <= 1.0 && shadowCoord.y >= 0.0 && shadowCoord.y <= 1.0;
			bool frustumTest = inFrustum && shadowCoord.z <= 1.0;
			if ( frustumTest ) {
				vec2 texelSize = vec2( 1.0 ) / shadowMapSize;
				float radius = shadowRadius * texelSize.x;
				float phi = interleavedGradientNoise( gl_FragCoord.xy ) * PI2;
				shadow = (
					texture( shadowMap, vec3( shadowCoord.xy + vogelDiskSample( 0, 5, phi ) * radius, shadowCoord.z ) ) +
					texture( shadowMap, vec3( shadowCoord.xy + vogelDiskSample( 1, 5, phi ) * radius, shadowCoord.z ) ) +
					texture( shadowMap, vec3( shadowCoord.xy + vogelDiskSample( 2, 5, phi ) * radius, shadowCoord.z ) ) +
					texture( shadowMap, vec3( shadowCoord.xy + vogelDiskSample( 3, 5, phi ) * radius, shadowCoord.z ) ) +
					texture( shadowMap, vec3( shadowCoord.xy + vogelDiskSample( 4, 5, phi ) * radius, shadowCoord.z ) )
				) * 0.2;
			}
			return mix( 1.0, shadow, shadowIntensity );
		}
	#elif defined( SHADOWMAP_TYPE_VSM )
		float getShadow( sampler2D shadowMap, vec2 shadowMapSize, float shadowIntensity, float shadowBias, float shadowRadius, vec4 shadowCoord ) {
			float shadow = 1.0;
			shadowCoord.xyz /= shadowCoord.w;
			#ifdef USE_REVERSED_DEPTH_BUFFER
				shadowCoord.z -= shadowBias;
			#else
				shadowCoord.z += shadowBias;
			#endif
			bool inFrustum = shadowCoord.x >= 0.0 && shadowCoord.x <= 1.0 && shadowCoord.y >= 0.0 && shadowCoord.y <= 1.0;
			bool frustumTest = inFrustum && shadowCoord.z <= 1.0;
			if ( frustumTest ) {
				vec2 distribution = texture2D( shadowMap, shadowCoord.xy ).rg;
				float mean = distribution.x;
				float variance = distribution.y * distribution.y;
				#ifdef USE_REVERSED_DEPTH_BUFFER
					float hard_shadow = step( mean, shadowCoord.z );
				#else
					float hard_shadow = step( shadowCoord.z, mean );
				#endif
				
				if ( hard_shadow == 1.0 ) {
					shadow = 1.0;
				} else {
					variance = max( variance, 0.0000001 );
					float d = shadowCoord.z - mean;
					float p_max = variance / ( variance + d * d );
					p_max = clamp( ( p_max - 0.3 ) / 0.65, 0.0, 1.0 );
					shadow = max( hard_shadow, p_max );
				}
			}
			return mix( 1.0, shadow, shadowIntensity );
		}
	#else
		float getShadow( sampler2D shadowMap, vec2 shadowMapSize, float shadowIntensity, float shadowBias, float shadowRadius, vec4 shadowCoord ) {
			float shadow = 1.0;
			shadowCoord.xyz /= shadowCoord.w;
			#ifdef USE_REVERSED_DEPTH_BUFFER
				shadowCoord.z -= shadowBias;
			#else
				shadowCoord.z += shadowBias;
			#endif
			bool inFrustum = shadowCoord.x >= 0.0 && shadowCoord.x <= 1.0 && shadowCoord.y >= 0.0 && shadowCoord.y <= 1.0;
			bool frustumTest = inFrustum && shadowCoord.z <= 1.0;
			if ( frustumTest ) {
				float depth = texture2D( shadowMap, shadowCoord.xy ).r;
				#ifdef USE_REVERSED_DEPTH_BUFFER
					shadow = step( depth, shadowCoord.z );
				#else
					shadow = step( shadowCoord.z, depth );
				#endif
			}
			return mix( 1.0, shadow, shadowIntensity );
		}
	#endif
	#if NUM_SUN_LIGHT_SHADOWS > 0
		float getSunShadow(
			#if defined( SHADOWMAP_TYPE_PCF )
				sampler2DShadow shadowMap,
			#else
				sampler2D shadowMap,
			#endif
			SunLightShadow sunLightShadow,
			int shadowIndex
		) {
			vec4 shadowWorldPosition = vec4( vSunShadowWorldPosition.xyz + vSunShadowWorldNormal * sunLightShadow.shadowNormalBias, 1.0 );
			float viewDepth = vSunShadowWorldPosition.w;
			int cascadeOffset = shadowIndex * SUN_LIGHT_CASCADES;
			float shadow = 1.0;
			for ( int i = SUN_LIGHT_CASCADES - 1; i >= 0; i -- ) {
				vec4 cascade = sunShadowCascade[ cascadeOffset + i ];
				if ( viewDepth >= cascade.x && viewDepth < cascade.y ) {
					float cascadeShadow = getShadow(
						shadowMap,
						sunLightShadow.shadowMapSize,
						sunLightShadow.shadowIntensity,
						sunLightShadow.shadowBias,
						sunLightShadow.shadowRadius,
						sunShadowMatrix[ cascadeOffset + i ] * shadowWorldPosition
					);
					shadow = mix( cascadeShadow, shadow, smoothstep( cascade.z, cascade.y, viewDepth ) );
				}
			}
			return shadow;
		}
	#endif
	#if NUM_POINT_LIGHT_SHADOWS > 0
	#if defined( SHADOWMAP_TYPE_PCF )
	float getPointShadow( samplerCubeShadow shadowMap, vec2 shadowMapSize, float shadowIntensity, float shadowBias, float shadowRadius, vec4 shadowCoord, float shadowCameraNear, float shadowCameraFar ) {
		float shadow = 1.0;
		vec3 lightToPosition = shadowCoord.xyz;
		vec3 bd3D = normalize( lightToPosition );
		vec3 absVec = abs( lightToPosition );
		float viewSpaceZ = max( max( absVec.x, absVec.y ), absVec.z );
		if ( viewSpaceZ - shadowCameraFar <= 0.0 && viewSpaceZ - shadowCameraNear >= 0.0 ) {
			#ifdef USE_REVERSED_DEPTH_BUFFER
				float dp = ( shadowCameraNear * ( shadowCameraFar - viewSpaceZ ) ) / ( viewSpaceZ * ( shadowCameraFar - shadowCameraNear ) );
				dp -= shadowBias;
			#else
				float dp = ( shadowCameraFar * ( viewSpaceZ - shadowCameraNear ) ) / ( viewSpaceZ * ( shadowCameraFar - shadowCameraNear ) );
				dp += shadowBias;
			#endif
			float texelSize = shadowRadius / shadowMapSize.x;
			vec3 absDir = abs( bd3D );
			vec3 tangent = absDir.x > absDir.z ? vec3( 0.0, 1.0, 0.0 ) : vec3( 1.0, 0.0, 0.0 );
			tangent = normalize( cross( bd3D, tangent ) );
			vec3 bitangent = cross( bd3D, tangent );
			float phi = interleavedGradientNoise( gl_FragCoord.xy ) * PI2;
			vec2 sample0 = vogelDiskSample( 0, 5, phi );
			vec2 sample1 = vogelDiskSample( 1, 5, phi );
			vec2 sample2 = vogelDiskSample( 2, 5, phi );
			vec2 sample3 = vogelDiskSample( 3, 5, phi );
			vec2 sample4 = vogelDiskSample( 4, 5, phi );
			shadow = (
				texture( shadowMap, vec4( bd3D + ( tangent * sample0.x + bitangent * sample0.y ) * texelSize, dp ) ) +
				texture( shadowMap, vec4( bd3D + ( tangent * sample1.x + bitangent * sample1.y ) * texelSize, dp ) ) +
				texture( shadowMap, vec4( bd3D + ( tangent * sample2.x + bitangent * sample2.y ) * texelSize, dp ) ) +
				texture( shadowMap, vec4( bd3D + ( tangent * sample3.x + bitangent * sample3.y ) * texelSize, dp ) ) +
				texture( shadowMap, vec4( bd3D + ( tangent * sample4.x + bitangent * sample4.y ) * texelSize, dp ) )
			) * 0.2;
		}
		return mix( 1.0, shadow, shadowIntensity );
	}
	#elif defined( SHADOWMAP_TYPE_BASIC )
	float getPointShadow( samplerCube shadowMap, vec2 shadowMapSize, float shadowIntensity, float shadowBias, float shadowRadius, vec4 shadowCoord, float shadowCameraNear, float shadowCameraFar ) {
		float shadow = 1.0;
		vec3 lightToPosition = shadowCoord.xyz;
		vec3 absVec = abs( lightToPosition );
		float viewSpaceZ = max( max( absVec.x, absVec.y ), absVec.z );
		if ( viewSpaceZ - shadowCameraFar <= 0.0 && viewSpaceZ - shadowCameraNear >= 0.0 ) {
			float dp = ( shadowCameraFar * ( viewSpaceZ - shadowCameraNear ) ) / ( viewSpaceZ * ( shadowCameraFar - shadowCameraNear ) );
			dp += shadowBias;
			vec3 bd3D = normalize( lightToPosition );
			float depth = textureCube( shadowMap, bd3D ).r;
			#ifdef USE_REVERSED_DEPTH_BUFFER
				depth = 1.0 - depth;
			#endif
			shadow = step( dp, depth );
		}
		return mix( 1.0, shadow, shadowIntensity );
	}
	#endif
	#endif
#endif`, g_ = `#if NUM_SPOT_LIGHT_COORDS > 0
	uniform mat4 spotLightMatrix[ NUM_SPOT_LIGHT_COORDS ];
	varying vec4 vSpotLightCoord[ NUM_SPOT_LIGHT_COORDS ];
#endif
#ifdef USE_SHADOWMAP
	#if NUM_SUN_LIGHT_SHADOWS > 0
		varying vec4 vSunShadowWorldPosition;
		varying vec3 vSunShadowWorldNormal;
	#endif
	#if NUM_DIR_LIGHT_SHADOWS > 0
		uniform mat4 directionalShadowMatrix[ NUM_DIR_LIGHT_SHADOWS ];
		varying vec4 vDirectionalShadowCoord[ NUM_DIR_LIGHT_SHADOWS ];
		struct DirectionalLightShadow {
			float shadowIntensity;
			float shadowBias;
			float shadowNormalBias;
			float shadowRadius;
			vec2 shadowMapSize;
		};
		uniform DirectionalLightShadow directionalLightShadows[ NUM_DIR_LIGHT_SHADOWS ];
	#endif
	#if NUM_SPOT_LIGHT_SHADOWS > 0
		struct SpotLightShadow {
			float shadowIntensity;
			float shadowBias;
			float shadowNormalBias;
			float shadowRadius;
			vec2 shadowMapSize;
		};
		uniform SpotLightShadow spotLightShadows[ NUM_SPOT_LIGHT_SHADOWS ];
	#endif
	#if NUM_POINT_LIGHT_SHADOWS > 0
		uniform mat4 pointShadowMatrix[ NUM_POINT_LIGHT_SHADOWS ];
		varying vec4 vPointShadowCoord[ NUM_POINT_LIGHT_SHADOWS ];
		struct PointLightShadow {
			float shadowIntensity;
			float shadowBias;
			float shadowNormalBias;
			float shadowRadius;
			vec2 shadowMapSize;
			float shadowCameraNear;
			float shadowCameraFar;
		};
		uniform PointLightShadow pointLightShadows[ NUM_POINT_LIGHT_SHADOWS ];
	#endif
#endif`, __ = `#if ( defined( USE_SHADOWMAP ) && ( NUM_DIR_LIGHT_SHADOWS > 0 || NUM_SUN_LIGHT_SHADOWS > 0 || NUM_POINT_LIGHT_SHADOWS > 0 ) ) || ( NUM_SPOT_LIGHT_COORDS > 0 )
	#ifdef HAS_NORMAL
		vec3 shadowWorldNormal = transformNormalByInverseViewMatrix( transformedNormal, viewMatrix );
	#else
		vec3 shadowWorldNormal = vec3( 0.0 );
	#endif
	vec4 shadowWorldPosition;
#endif
#if defined( USE_SHADOWMAP )
	#if NUM_SUN_LIGHT_SHADOWS > 0
		vSunShadowWorldPosition = vec4( worldPosition.xyz, - mvPosition.z );
		vSunShadowWorldNormal = shadowWorldNormal;
	#endif
	#if NUM_DIR_LIGHT_SHADOWS > 0
		#pragma unroll_loop_start
		for ( int i = 0; i < NUM_DIR_LIGHT_SHADOWS; i ++ ) {
			shadowWorldPosition = worldPosition + vec4( shadowWorldNormal * directionalLightShadows[ i ].shadowNormalBias, 0 );
			vDirectionalShadowCoord[ i ] = directionalShadowMatrix[ i ] * shadowWorldPosition;
		}
		#pragma unroll_loop_end
	#endif
	#if NUM_POINT_LIGHT_SHADOWS > 0
		#pragma unroll_loop_start
		for ( int i = 0; i < NUM_POINT_LIGHT_SHADOWS; i ++ ) {
			shadowWorldPosition = worldPosition + vec4( shadowWorldNormal * pointLightShadows[ i ].shadowNormalBias, 0 );
			vPointShadowCoord[ i ] = pointShadowMatrix[ i ] * shadowWorldPosition;
		}
		#pragma unroll_loop_end
	#endif
#endif
#if NUM_SPOT_LIGHT_COORDS > 0
	#pragma unroll_loop_start
	for ( int i = 0; i < NUM_SPOT_LIGHT_COORDS; i ++ ) {
		shadowWorldPosition = worldPosition;
		#if ( defined( USE_SHADOWMAP ) && UNROLLED_LOOP_INDEX < NUM_SPOT_LIGHT_SHADOWS )
			shadowWorldPosition.xyz += shadowWorldNormal * spotLightShadows[ i ].shadowNormalBias;
		#endif
		vSpotLightCoord[ i ] = spotLightMatrix[ i ] * shadowWorldPosition;
	}
	#pragma unroll_loop_end
#endif`, v_ = `float getShadowMask() {
	float shadow = 1.0;
	#ifdef USE_SHADOWMAP
	#if NUM_SUN_LIGHT_SHADOWS > 0
	SunLightShadow sunLight;
	#pragma unroll_loop_start
	for ( int i = 0; i < NUM_SUN_LIGHT_SHADOWS; i ++ ) {
		sunLight = sunLightShadows[ i ];
		shadow *= receiveShadow ? getSunShadow( sunShadowMap[ i ], sunLight, UNROLLED_LOOP_INDEX ) : 1.0;
	}
	#pragma unroll_loop_end
	#endif
	#if NUM_DIR_LIGHT_SHADOWS > 0
	DirectionalLightShadow directionalLight;
	#pragma unroll_loop_start
	for ( int i = 0; i < NUM_DIR_LIGHT_SHADOWS; i ++ ) {
		directionalLight = directionalLightShadows[ i ];
		shadow *= receiveShadow ? getShadow( directionalShadowMap[ i ], directionalLight.shadowMapSize, directionalLight.shadowIntensity, directionalLight.shadowBias, directionalLight.shadowRadius, vDirectionalShadowCoord[ i ] ) : 1.0;
	}
	#pragma unroll_loop_end
	#endif
	#if NUM_SPOT_LIGHT_SHADOWS > 0
	SpotLightShadow spotLight;
	#pragma unroll_loop_start
	for ( int i = 0; i < NUM_SPOT_LIGHT_SHADOWS; i ++ ) {
		spotLight = spotLightShadows[ i ];
		shadow *= receiveShadow ? getShadow( spotShadowMap[ i ], spotLight.shadowMapSize, spotLight.shadowIntensity, spotLight.shadowBias, spotLight.shadowRadius, vSpotLightCoord[ i ] ) : 1.0;
	}
	#pragma unroll_loop_end
	#endif
	#if NUM_POINT_LIGHT_SHADOWS > 0 && ( defined( SHADOWMAP_TYPE_PCF ) || defined( SHADOWMAP_TYPE_BASIC ) )
	PointLightShadow pointLight;
	#pragma unroll_loop_start
	for ( int i = 0; i < NUM_POINT_LIGHT_SHADOWS; i ++ ) {
		pointLight = pointLightShadows[ i ];
		shadow *= receiveShadow ? getPointShadow( pointShadowMap[ i ], pointLight.shadowMapSize, pointLight.shadowIntensity, pointLight.shadowBias, pointLight.shadowRadius, vPointShadowCoord[ i ], pointLight.shadowCameraNear, pointLight.shadowCameraFar ) : 1.0;
	}
	#pragma unroll_loop_end
	#endif
	#endif
	return shadow;
}`, x_ = `#ifdef USE_SKINNING
	mat4 boneMatX = getBoneMatrix( skinIndex.x );
	mat4 boneMatY = getBoneMatrix( skinIndex.y );
	mat4 boneMatZ = getBoneMatrix( skinIndex.z );
	mat4 boneMatW = getBoneMatrix( skinIndex.w );
#endif`, M_ = `#ifdef USE_SKINNING
	uniform mat4 bindMatrix;
	uniform mat4 bindMatrixInverse;
	uniform highp sampler2D boneTexture;
	mat4 getBoneMatrix( const in float i ) {
		int size = textureSize( boneTexture, 0 ).x;
		int j = int( i ) * 4;
		int x = j % size;
		int y = j / size;
		vec4 v1 = texelFetch( boneTexture, ivec2( x, y ), 0 );
		vec4 v2 = texelFetch( boneTexture, ivec2( x + 1, y ), 0 );
		vec4 v3 = texelFetch( boneTexture, ivec2( x + 2, y ), 0 );
		vec4 v4 = texelFetch( boneTexture, ivec2( x + 3, y ), 0 );
		return mat4( v1, v2, v3, v4 );
	}
#endif`, y_ = `#ifdef USE_SKINNING
	vec4 skinVertex = bindMatrix * vec4( transformed, 1.0 );
	vec4 skinned = vec4( 0.0 );
	skinned += boneMatX * skinVertex * skinWeight.x;
	skinned += boneMatY * skinVertex * skinWeight.y;
	skinned += boneMatZ * skinVertex * skinWeight.z;
	skinned += boneMatW * skinVertex * skinWeight.w;
	transformed = ( bindMatrixInverse * skinned ).xyz;
#endif`, S_ = `#ifdef USE_SKINNING
	mat4 skinMatrix = mat4( 0.0 );
	skinMatrix += skinWeight.x * boneMatX;
	skinMatrix += skinWeight.y * boneMatY;
	skinMatrix += skinWeight.z * boneMatZ;
	skinMatrix += skinWeight.w * boneMatW;
	skinMatrix = bindMatrixInverse * skinMatrix * bindMatrix;
	objectNormal = vec4( skinMatrix * vec4( objectNormal, 0.0 ) ).xyz;
	#ifdef USE_TANGENT
		objectTangent = vec4( skinMatrix * vec4( objectTangent, 0.0 ) ).xyz;
	#endif
#endif`, T_ = `float specularStrength;
#ifdef USE_SPECULARMAP
	vec4 texelSpecular = texture2D( specularMap, vSpecularMapUv );
	specularStrength = texelSpecular.r;
#else
	specularStrength = 1.0;
#endif`, E_ = `#ifdef USE_SPECULARMAP
	uniform sampler2D specularMap;
#endif`, b_ = `#if defined( TONE_MAPPING )
	gl_FragColor.rgb = toneMapping( gl_FragColor.rgb );
#endif`, w_ = `#ifndef saturate
#define saturate( a ) clamp( a, 0.0, 1.0 )
#endif
uniform float toneMappingExposure;
vec3 LinearToneMapping( vec3 color ) {
	return saturate( toneMappingExposure * color );
}
vec3 ReinhardToneMapping( vec3 color ) {
	color *= toneMappingExposure;
	return saturate( color / ( vec3( 1.0 ) + color ) );
}
vec3 CineonToneMapping( vec3 color ) {
	color *= toneMappingExposure;
	color = max( vec3( 0.0 ), color - 0.004 );
	return pow( ( color * ( 6.2 * color + 0.5 ) ) / ( color * ( 6.2 * color + 1.7 ) + 0.06 ), vec3( 2.2 ) );
}
vec3 RRTAndODTFit( vec3 v ) {
	vec3 a = v * ( v + 0.0245786 ) - 0.000090537;
	vec3 b = v * ( 0.983729 * v + 0.4329510 ) + 0.238081;
	return a / b;
}
vec3 ACESFilmicToneMapping( vec3 color ) {
	const mat3 ACESInputMat = mat3(
		vec3( 0.59719, 0.07600, 0.02840 ),		vec3( 0.35458, 0.90834, 0.13383 ),
		vec3( 0.04823, 0.01566, 0.83777 )
	);
	const mat3 ACESOutputMat = mat3(
		vec3(  1.60475, -0.10208, -0.00327 ),		vec3( -0.53108,  1.10813, -0.07276 ),
		vec3( -0.07367, -0.00605,  1.07602 )
	);
	color *= toneMappingExposure / 0.6;
	color = ACESInputMat * color;
	color = RRTAndODTFit( color );
	color = ACESOutputMat * color;
	return saturate( color );
}
const mat3 LINEAR_REC2020_TO_LINEAR_SRGB = mat3(
	vec3( 1.6605, - 0.1246, - 0.0182 ),
	vec3( - 0.5876, 1.1329, - 0.1006 ),
	vec3( - 0.0728, - 0.0083, 1.1187 )
);
const mat3 LINEAR_SRGB_TO_LINEAR_REC2020 = mat3(
	vec3( 0.6274, 0.0691, 0.0164 ),
	vec3( 0.3293, 0.9195, 0.0880 ),
	vec3( 0.0433, 0.0113, 0.8956 )
);
vec3 agxDefaultContrastApprox( vec3 x ) {
	vec3 x2 = x * x;
	vec3 x4 = x2 * x2;
	return + 15.5 * x4 * x2
		- 40.14 * x4 * x
		+ 31.96 * x4
		- 6.868 * x2 * x
		+ 0.4298 * x2
		+ 0.1191 * x
		- 0.00232;
}
vec3 AgXToneMapping( vec3 color ) {
	const mat3 AgXInsetMatrix = mat3(
		vec3( 0.856627153315983, 0.137318972929847, 0.11189821299995 ),
		vec3( 0.0951212405381588, 0.761241990602591, 0.0767994186031903 ),
		vec3( 0.0482516061458583, 0.101439036467562, 0.811302368396859 )
	);
	const mat3 AgXOutsetMatrix = mat3(
		vec3( 1.1271005818144368, - 0.1413297634984383, - 0.14132976349843826 ),
		vec3( - 0.11060664309660323, 1.157823702216272, - 0.11060664309660294 ),
		vec3( - 0.016493938717834573, - 0.016493938717834257, 1.2519364065950405 )
	);
	const float AgxMinEv = - 12.47393;	const float AgxMaxEv = 4.026069;
	color *= toneMappingExposure;
	color = LINEAR_SRGB_TO_LINEAR_REC2020 * color;
	color = AgXInsetMatrix * color;
	color = max( color, 1e-10 );	color = log2( color );
	color = ( color - AgxMinEv ) / ( AgxMaxEv - AgxMinEv );
	color = clamp( color, 0.0, 1.0 );
	color = agxDefaultContrastApprox( color );
	color = AgXOutsetMatrix * color;
	color = pow( max( vec3( 0.0 ), color ), vec3( 2.2 ) );
	color = LINEAR_REC2020_TO_LINEAR_SRGB * color;
	color = clamp( color, 0.0, 1.0 );
	return color;
}
vec3 NeutralToneMapping( vec3 color ) {
	const float StartCompression = 0.8 - 0.04;
	const float Desaturation = 0.15;
	color *= toneMappingExposure;
	float x = min( color.r, min( color.g, color.b ) );
	float offset = x < 0.08 ? x - 6.25 * x * x : 0.04;
	color -= offset;
	float peak = max( color.r, max( color.g, color.b ) );
	if ( peak < StartCompression ) return color;
	float d = 1. - StartCompression;
	float newPeak = 1. - d * d / ( peak + d - StartCompression );
	color *= newPeak / peak;
	float g = 1. - 1. / ( Desaturation * ( peak - newPeak ) + 1. );
	return mix( color, vec3( newPeak ), g );
}
vec3 CustomToneMapping( vec3 color ) { return color; }`, A_ = `#ifdef USE_TRANSMISSION
	material.transmission = transmission;
	material.transmissionAlpha = 1.0;
	material.thickness = thickness;
	material.attenuationDistance = attenuationDistance;
	material.attenuationColor = attenuationColor;
	#ifdef USE_TRANSMISSIONMAP
		material.transmission *= texture2D( transmissionMap, vTransmissionMapUv ).r;
	#endif
	#ifdef USE_THICKNESSMAP
		material.thickness *= texture2D( thicknessMap, vThicknessMapUv ).g;
	#endif
	vec3 pos = vWorldPosition;
	vec3 v = normalize( cameraPosition - pos );
	vec3 n = transformNormalByInverseViewMatrix( normal, viewMatrix );
	vec4 transmitted = getIBLVolumeRefraction(
		n, v, material.roughness, material.diffuseContribution, material.specularColorBlended, material.specularF90,
		pos, modelMatrix, viewMatrix, projectionMatrix, material.dispersion, material.ior, material.thickness,
		material.attenuationColor, material.attenuationDistance );
	material.transmissionAlpha = mix( material.transmissionAlpha, transmitted.a, material.transmission );
	totalDiffuse = mix( totalDiffuse, transmitted.rgb, material.transmission );
#endif`, R_ = `#ifdef USE_TRANSMISSION
	uniform float transmission;
	uniform float thickness;
	uniform float attenuationDistance;
	uniform vec3 attenuationColor;
	#ifdef USE_TRANSMISSIONMAP
		uniform sampler2D transmissionMap;
	#endif
	#ifdef USE_THICKNESSMAP
		uniform sampler2D thicknessMap;
	#endif
	uniform vec2 transmissionSamplerSize;
	uniform sampler2D transmissionSamplerMap;
	uniform mat4 modelMatrix;
	uniform mat4 projectionMatrix;
	varying vec3 vWorldPosition;
	float w0( float a ) {
		return ( 1.0 / 6.0 ) * ( a * ( a * ( - a + 3.0 ) - 3.0 ) + 1.0 );
	}
	float w1( float a ) {
		return ( 1.0 / 6.0 ) * ( a *  a * ( 3.0 * a - 6.0 ) + 4.0 );
	}
	float w2( float a ){
		return ( 1.0 / 6.0 ) * ( a * ( a * ( - 3.0 * a + 3.0 ) + 3.0 ) + 1.0 );
	}
	float w3( float a ) {
		return ( 1.0 / 6.0 ) * ( a * a * a );
	}
	float g0( float a ) {
		return w0( a ) + w1( a );
	}
	float g1( float a ) {
		return w2( a ) + w3( a );
	}
	float h0( float a ) {
		return - 1.0 + w1( a ) / ( w0( a ) + w1( a ) );
	}
	float h1( float a ) {
		return 1.0 + w3( a ) / ( w2( a ) + w3( a ) );
	}
	vec4 bicubic( sampler2D tex, vec2 uv, vec4 texelSize, float lod ) {
		uv = uv * texelSize.zw + 0.5;
		vec2 iuv = floor( uv );
		vec2 fuv = fract( uv );
		float g0x = g0( fuv.x );
		float g1x = g1( fuv.x );
		float h0x = h0( fuv.x );
		float h1x = h1( fuv.x );
		float h0y = h0( fuv.y );
		float h1y = h1( fuv.y );
		vec2 p0 = ( vec2( iuv.x + h0x, iuv.y + h0y ) - 0.5 ) * texelSize.xy;
		vec2 p1 = ( vec2( iuv.x + h1x, iuv.y + h0y ) - 0.5 ) * texelSize.xy;
		vec2 p2 = ( vec2( iuv.x + h0x, iuv.y + h1y ) - 0.5 ) * texelSize.xy;
		vec2 p3 = ( vec2( iuv.x + h1x, iuv.y + h1y ) - 0.5 ) * texelSize.xy;
		return g0( fuv.y ) * ( g0x * textureLod( tex, p0, lod ) + g1x * textureLod( tex, p1, lod ) ) +
			g1( fuv.y ) * ( g0x * textureLod( tex, p2, lod ) + g1x * textureLod( tex, p3, lod ) );
	}
	vec4 textureBicubic( sampler2D sampler, vec2 uv, float lod ) {
		vec2 fLodSize = vec2( textureSize( sampler, int( lod ) ) );
		vec2 cLodSize = vec2( textureSize( sampler, int( lod + 1.0 ) ) );
		vec2 fLodSizeInv = 1.0 / fLodSize;
		vec2 cLodSizeInv = 1.0 / cLodSize;
		vec4 fSample = bicubic( sampler, uv, vec4( fLodSizeInv, fLodSize ), floor( lod ) );
		vec4 cSample = bicubic( sampler, uv, vec4( cLodSizeInv, cLodSize ), ceil( lod ) );
		return mix( fSample, cSample, fract( lod ) );
	}
	vec3 getVolumeTransmissionRay( const in vec3 n, const in vec3 v, const in float thickness, const in float ior, const in mat4 modelMatrix ) {
		vec3 refractionVector = refract( - v, normalize( n ), 1.0 / ior );
		vec3 modelScale;
		modelScale.x = length( vec3( modelMatrix[ 0 ].xyz ) );
		modelScale.y = length( vec3( modelMatrix[ 1 ].xyz ) );
		modelScale.z = length( vec3( modelMatrix[ 2 ].xyz ) );
		return normalize( refractionVector ) * thickness * modelScale;
	}
	float applyIorToRoughness( const in float roughness, const in float ior ) {
		return roughness * clamp( ior * 2.0 - 2.0, 0.0, 1.0 );
	}
	vec4 getTransmissionSample( const in vec2 fragCoord, const in float roughness, const in float ior ) {
		float lod = log2( transmissionSamplerSize.x ) * applyIorToRoughness( roughness, ior );
		return textureBicubic( transmissionSamplerMap, fragCoord.xy, lod );
	}
	vec3 volumeAttenuation( const in float transmissionDistance, const in vec3 attenuationColor, const in float attenuationDistance ) {
		if ( isinf( attenuationDistance ) ) {
			return vec3( 1.0 );
		} else {
			vec3 attenuationCoefficient = -log( attenuationColor ) / attenuationDistance;
			vec3 transmittance = exp( - attenuationCoefficient * transmissionDistance );			return transmittance;
		}
	}
	vec4 getIBLVolumeRefraction( const in vec3 n, const in vec3 v, const in float roughness, const in vec3 diffuseColor,
		const in vec3 specularColor, const in float specularF90, const in vec3 position, const in mat4 modelMatrix,
		const in mat4 viewMatrix, const in mat4 projMatrix, const in float dispersion, const in float ior, const in float thickness,
		const in vec3 attenuationColor, const in float attenuationDistance ) {
		vec4 transmittedLight;
		vec3 transmittance;
		#ifdef USE_DISPERSION
			float halfSpread = ( ior - 1.0 ) * 0.025 * dispersion;
			vec3 iors = vec3( ior - halfSpread, ior, ior + halfSpread );
			for ( int i = 0; i < 3; i ++ ) {
				vec3 transmissionRay = getVolumeTransmissionRay( n, v, thickness, iors[ i ], modelMatrix );
				vec3 refractedRayExit = position + transmissionRay;
				vec4 ndcPos = projMatrix * viewMatrix * vec4( refractedRayExit, 1.0 );
				vec2 refractionCoords = ndcPos.xy / ndcPos.w;
				refractionCoords += 1.0;
				refractionCoords /= 2.0;
				vec4 transmissionSample = getTransmissionSample( refractionCoords, roughness, iors[ i ] );
				transmittedLight[ i ] = transmissionSample[ i ];
				transmittedLight.a += transmissionSample.a;
				transmittance[ i ] = diffuseColor[ i ] * volumeAttenuation( length( transmissionRay ), attenuationColor, attenuationDistance )[ i ];
			}
			transmittedLight.a /= 3.0;
		#else
			vec3 transmissionRay = getVolumeTransmissionRay( n, v, thickness, ior, modelMatrix );
			vec3 refractedRayExit = position + transmissionRay;
			vec4 ndcPos = projMatrix * viewMatrix * vec4( refractedRayExit, 1.0 );
			vec2 refractionCoords = ndcPos.xy / ndcPos.w;
			refractionCoords += 1.0;
			refractionCoords /= 2.0;
			transmittedLight = getTransmissionSample( refractionCoords, roughness, ior );
			transmittance = diffuseColor * volumeAttenuation( length( transmissionRay ), attenuationColor, attenuationDistance );
		#endif
		vec3 attenuatedColor = transmittance * transmittedLight.rgb;
		vec3 F = EnvironmentBRDF( n, v, specularColor, specularF90, roughness );
		float transmittanceFactor = ( transmittance.r + transmittance.g + transmittance.b ) / 3.0;
		return vec4( ( 1.0 - F ) * attenuatedColor, 1.0 - ( 1.0 - transmittedLight.a ) * transmittanceFactor );
	}
#endif`, P_ = `#if defined( USE_UV ) || defined( USE_ANISOTROPY )
	varying vec2 vUv;
#endif
#ifdef USE_MAP
	varying vec2 vMapUv;
#endif
#ifdef USE_ALPHAMAP
	varying vec2 vAlphaMapUv;
#endif
#ifdef USE_LIGHTMAP
	varying vec2 vLightMapUv;
#endif
#ifdef USE_AOMAP
	varying vec2 vAoMapUv;
#endif
#ifdef USE_BUMPMAP
	varying vec2 vBumpMapUv;
#endif
#ifdef USE_NORMALMAP
	varying vec2 vNormalMapUv;
#endif
#ifdef USE_EMISSIVEMAP
	varying vec2 vEmissiveMapUv;
#endif
#ifdef USE_METALNESSMAP
	varying vec2 vMetalnessMapUv;
#endif
#ifdef USE_ROUGHNESSMAP
	varying vec2 vRoughnessMapUv;
#endif
#ifdef USE_ANISOTROPYMAP
	varying vec2 vAnisotropyMapUv;
#endif
#ifdef USE_CLEARCOATMAP
	varying vec2 vClearcoatMapUv;
#endif
#ifdef USE_CLEARCOAT_NORMALMAP
	varying vec2 vClearcoatNormalMapUv;
#endif
#ifdef USE_CLEARCOAT_ROUGHNESSMAP
	varying vec2 vClearcoatRoughnessMapUv;
#endif
#ifdef USE_IRIDESCENCEMAP
	varying vec2 vIridescenceMapUv;
#endif
#ifdef USE_IRIDESCENCE_THICKNESSMAP
	varying vec2 vIridescenceThicknessMapUv;
#endif
#ifdef USE_SHEEN_COLORMAP
	varying vec2 vSheenColorMapUv;
#endif
#ifdef USE_SHEEN_ROUGHNESSMAP
	varying vec2 vSheenRoughnessMapUv;
#endif
#ifdef USE_SPECULARMAP
	varying vec2 vSpecularMapUv;
#endif
#ifdef USE_SPECULAR_COLORMAP
	varying vec2 vSpecularColorMapUv;
#endif
#ifdef USE_SPECULAR_INTENSITYMAP
	varying vec2 vSpecularIntensityMapUv;
#endif
#ifdef USE_TRANSMISSIONMAP
	uniform mat3 transmissionMapTransform;
	varying vec2 vTransmissionMapUv;
#endif
#ifdef USE_THICKNESSMAP
	uniform mat3 thicknessMapTransform;
	varying vec2 vThicknessMapUv;
#endif`, C_ = `#if defined( USE_UV ) || defined( USE_ANISOTROPY )
	varying vec2 vUv;
#endif
#ifdef USE_MAP
	uniform mat3 mapTransform;
	varying vec2 vMapUv;
#endif
#ifdef USE_ALPHAMAP
	uniform mat3 alphaMapTransform;
	varying vec2 vAlphaMapUv;
#endif
#ifdef USE_LIGHTMAP
	uniform mat3 lightMapTransform;
	varying vec2 vLightMapUv;
#endif
#ifdef USE_AOMAP
	uniform mat3 aoMapTransform;
	varying vec2 vAoMapUv;
#endif
#ifdef USE_BUMPMAP
	uniform mat3 bumpMapTransform;
	varying vec2 vBumpMapUv;
#endif
#ifdef USE_NORMALMAP
	uniform mat3 normalMapTransform;
	varying vec2 vNormalMapUv;
#endif
#ifdef USE_DISPLACEMENTMAP
	uniform mat3 displacementMapTransform;
	varying vec2 vDisplacementMapUv;
#endif
#ifdef USE_EMISSIVEMAP
	uniform mat3 emissiveMapTransform;
	varying vec2 vEmissiveMapUv;
#endif
#ifdef USE_METALNESSMAP
	uniform mat3 metalnessMapTransform;
	varying vec2 vMetalnessMapUv;
#endif
#ifdef USE_ROUGHNESSMAP
	uniform mat3 roughnessMapTransform;
	varying vec2 vRoughnessMapUv;
#endif
#ifdef USE_ANISOTROPYMAP
	uniform mat3 anisotropyMapTransform;
	varying vec2 vAnisotropyMapUv;
#endif
#ifdef USE_CLEARCOATMAP
	uniform mat3 clearcoatMapTransform;
	varying vec2 vClearcoatMapUv;
#endif
#ifdef USE_CLEARCOAT_NORMALMAP
	uniform mat3 clearcoatNormalMapTransform;
	varying vec2 vClearcoatNormalMapUv;
#endif
#ifdef USE_CLEARCOAT_ROUGHNESSMAP
	uniform mat3 clearcoatRoughnessMapTransform;
	varying vec2 vClearcoatRoughnessMapUv;
#endif
#ifdef USE_SHEEN_COLORMAP
	uniform mat3 sheenColorMapTransform;
	varying vec2 vSheenColorMapUv;
#endif
#ifdef USE_SHEEN_ROUGHNESSMAP
	uniform mat3 sheenRoughnessMapTransform;
	varying vec2 vSheenRoughnessMapUv;
#endif
#ifdef USE_IRIDESCENCEMAP
	uniform mat3 iridescenceMapTransform;
	varying vec2 vIridescenceMapUv;
#endif
#ifdef USE_IRIDESCENCE_THICKNESSMAP
	uniform mat3 iridescenceThicknessMapTransform;
	varying vec2 vIridescenceThicknessMapUv;
#endif
#ifdef USE_SPECULARMAP
	uniform mat3 specularMapTransform;
	varying vec2 vSpecularMapUv;
#endif
#ifdef USE_SPECULAR_COLORMAP
	uniform mat3 specularColorMapTransform;
	varying vec2 vSpecularColorMapUv;
#endif
#ifdef USE_SPECULAR_INTENSITYMAP
	uniform mat3 specularIntensityMapTransform;
	varying vec2 vSpecularIntensityMapUv;
#endif
#ifdef USE_TRANSMISSIONMAP
	uniform mat3 transmissionMapTransform;
	varying vec2 vTransmissionMapUv;
#endif
#ifdef USE_THICKNESSMAP
	uniform mat3 thicknessMapTransform;
	varying vec2 vThicknessMapUv;
#endif`, L_ = `#if defined( USE_UV ) || defined( USE_ANISOTROPY )
	vUv = vec3( uv, 1 ).xy;
#endif
#ifdef USE_MAP
	vMapUv = ( mapTransform * vec3( MAP_UV, 1 ) ).xy;
#endif
#ifdef USE_ALPHAMAP
	vAlphaMapUv = ( alphaMapTransform * vec3( ALPHAMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_LIGHTMAP
	vLightMapUv = ( lightMapTransform * vec3( LIGHTMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_AOMAP
	vAoMapUv = ( aoMapTransform * vec3( AOMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_BUMPMAP
	vBumpMapUv = ( bumpMapTransform * vec3( BUMPMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_NORMALMAP
	vNormalMapUv = ( normalMapTransform * vec3( NORMALMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_DISPLACEMENTMAP
	vDisplacementMapUv = ( displacementMapTransform * vec3( DISPLACEMENTMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_EMISSIVEMAP
	vEmissiveMapUv = ( emissiveMapTransform * vec3( EMISSIVEMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_METALNESSMAP
	vMetalnessMapUv = ( metalnessMapTransform * vec3( METALNESSMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_ROUGHNESSMAP
	vRoughnessMapUv = ( roughnessMapTransform * vec3( ROUGHNESSMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_ANISOTROPYMAP
	vAnisotropyMapUv = ( anisotropyMapTransform * vec3( ANISOTROPYMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_CLEARCOATMAP
	vClearcoatMapUv = ( clearcoatMapTransform * vec3( CLEARCOATMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_CLEARCOAT_NORMALMAP
	vClearcoatNormalMapUv = ( clearcoatNormalMapTransform * vec3( CLEARCOAT_NORMALMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_CLEARCOAT_ROUGHNESSMAP
	vClearcoatRoughnessMapUv = ( clearcoatRoughnessMapTransform * vec3( CLEARCOAT_ROUGHNESSMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_IRIDESCENCEMAP
	vIridescenceMapUv = ( iridescenceMapTransform * vec3( IRIDESCENCEMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_IRIDESCENCE_THICKNESSMAP
	vIridescenceThicknessMapUv = ( iridescenceThicknessMapTransform * vec3( IRIDESCENCE_THICKNESSMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_SHEEN_COLORMAP
	vSheenColorMapUv = ( sheenColorMapTransform * vec3( SHEEN_COLORMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_SHEEN_ROUGHNESSMAP
	vSheenRoughnessMapUv = ( sheenRoughnessMapTransform * vec3( SHEEN_ROUGHNESSMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_SPECULARMAP
	vSpecularMapUv = ( specularMapTransform * vec3( SPECULARMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_SPECULAR_COLORMAP
	vSpecularColorMapUv = ( specularColorMapTransform * vec3( SPECULAR_COLORMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_SPECULAR_INTENSITYMAP
	vSpecularIntensityMapUv = ( specularIntensityMapTransform * vec3( SPECULAR_INTENSITYMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_TRANSMISSIONMAP
	vTransmissionMapUv = ( transmissionMapTransform * vec3( TRANSMISSIONMAP_UV, 1 ) ).xy;
#endif
#ifdef USE_THICKNESSMAP
	vThicknessMapUv = ( thicknessMapTransform * vec3( THICKNESSMAP_UV, 1 ) ).xy;
#endif`, I_ = `#if defined( USE_ENVMAP ) || defined( DISTANCE ) || defined ( USE_SHADOWMAP ) || defined ( USE_TRANSMISSION ) || NUM_SPOT_LIGHT_COORDS > 0
	vec4 worldPosition = vec4( transformed, 1.0 );
	#ifdef USE_BATCHING
		worldPosition = batchingMatrix * worldPosition;
	#endif
	#ifdef USE_INSTANCING
		worldPosition = instanceMatrix * worldPosition;
	#endif
	worldPosition = modelMatrix * worldPosition;
#endif`;
const N_ = `varying vec2 vUv;
uniform mat3 uvTransform;
void main() {
	vUv = ( uvTransform * vec3( uv, 1 ) ).xy;
	gl_Position = vec4( position.xy, 1.0, 1.0 );
}`, U_ = `uniform sampler2D t2D;
uniform float backgroundIntensity;
varying vec2 vUv;
void main() {
	vec4 texColor = texture2D( t2D, vUv );
	#ifdef DECODE_VIDEO_TEXTURE
		texColor = vec4( mix( pow( texColor.rgb * 0.9478672986 + vec3( 0.0521327014 ), vec3( 2.4 ) ), texColor.rgb * 0.0773993808, vec3( lessThanEqual( texColor.rgb, vec3( 0.04045 ) ) ) ), texColor.w );
	#endif
	texColor.rgb *= backgroundIntensity;
	gl_FragColor = texColor;
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
}`, D_ = `varying vec3 vWorldDirection;
#include <common>
void main() {
	vWorldDirection = transformDirection( position, modelMatrix );
	#include <begin_vertex>
	#include <project_vertex>
	gl_Position.z = gl_Position.w;
}`, O_ = `#ifdef ENVMAP_TYPE_CUBE
	uniform samplerCube envMap;
#elif defined( ENVMAP_TYPE_CUBE_UV )
	uniform sampler2D envMap;
#endif
uniform float backgroundBlurriness;
uniform float backgroundIntensity;
uniform mat3 backgroundRotation;
varying vec3 vWorldDirection;
#include <cube_uv_reflection_fragment>
void main() {
	#ifdef ENVMAP_TYPE_CUBE
		vec4 texColor = textureCube( envMap, backgroundRotation * vWorldDirection );
	#elif defined( ENVMAP_TYPE_CUBE_UV )
		vec4 texColor = textureCubeUV( envMap, backgroundRotation * vWorldDirection, backgroundBlurriness );
	#else
		vec4 texColor = vec4( 0.0, 0.0, 0.0, 1.0 );
	#endif
	texColor.rgb *= backgroundIntensity;
	gl_FragColor = texColor;
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
}`, F_ = `varying vec3 vWorldDirection;
#include <common>
void main() {
	vWorldDirection = transformDirection( position, modelMatrix );
	#include <begin_vertex>
	#include <project_vertex>
	gl_Position.z = gl_Position.w;
}`, B_ = `uniform samplerCube tCube;
uniform float tFlip;
uniform float opacity;
varying vec3 vWorldDirection;
void main() {
	vec4 texColor = textureCube( tCube, vec3( tFlip * vWorldDirection.x, vWorldDirection.yz ) );
	gl_FragColor = texColor;
	gl_FragColor.a *= opacity;
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
}`, V_ = `#include <common>
#include <batching_pars_vertex>
#include <uv_pars_vertex>
#include <displacementmap_pars_vertex>
#include <morphtarget_pars_vertex>
#include <skinning_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>
varying vec2 vHighPrecisionZW;
void main() {
	#include <uv_vertex>
	#include <batching_vertex>
	#include <skinbase_vertex>
	#include <morphinstance_vertex>
	#ifdef USE_DISPLACEMENTMAP
		#include <beginnormal_vertex>
		#include <morphnormal_vertex>
		#include <skinnormal_vertex>
	#endif
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <skinning_vertex>
	#include <displacementmap_vertex>
	#include <project_vertex>
	#include <logdepthbuf_vertex>
	#include <clipping_planes_vertex>
	vHighPrecisionZW = gl_Position.zw;
}`, H_ = `#if DEPTH_PACKING == 3200
	uniform float opacity;
#endif
#include <common>
#include <packing>
#include <uv_pars_fragment>
#include <map_pars_fragment>
#include <alphamap_pars_fragment>
#include <alphatest_pars_fragment>
#include <alphahash_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>
varying vec2 vHighPrecisionZW;
void main() {
	vec4 diffuseColor = vec4( 1.0 );
	#include <clipping_planes_fragment>
	#if DEPTH_PACKING == 3200
		diffuseColor.a = opacity;
	#endif
	#include <map_fragment>
	#include <alphamap_fragment>
	#include <alphatest_fragment>
	#include <alphahash_fragment>
	#include <logdepthbuf_fragment>
	#ifdef USE_REVERSED_DEPTH_BUFFER
		float fragCoordZ = vHighPrecisionZW[ 0 ] / vHighPrecisionZW[ 1 ];
	#else
		float fragCoordZ = 0.5 * vHighPrecisionZW[ 0 ] / vHighPrecisionZW[ 1 ] + 0.5;
	#endif
	#if DEPTH_PACKING == 3200
		gl_FragColor = vec4( vec3( 1.0 - fragCoordZ ), opacity );
	#elif DEPTH_PACKING == 3201
		gl_FragColor = packDepthToRGBA( fragCoordZ );
	#elif DEPTH_PACKING == 3202
		gl_FragColor = vec4( packDepthToRGB( fragCoordZ ), 1.0 );
	#elif DEPTH_PACKING == 3203
		gl_FragColor = vec4( packDepthToRG( fragCoordZ ), 0.0, 1.0 );
	#endif
}`, k_ = `#define DISTANCE
varying vec3 vWorldPosition;
#include <common>
#include <batching_pars_vertex>
#include <uv_pars_vertex>
#include <displacementmap_pars_vertex>
#include <morphtarget_pars_vertex>
#include <skinning_pars_vertex>
#include <clipping_planes_pars_vertex>
void main() {
	#include <uv_vertex>
	#include <batching_vertex>
	#include <skinbase_vertex>
	#include <morphinstance_vertex>
	#ifdef USE_DISPLACEMENTMAP
		#include <beginnormal_vertex>
		#include <morphnormal_vertex>
		#include <skinnormal_vertex>
	#endif
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <skinning_vertex>
	#include <displacementmap_vertex>
	#include <project_vertex>
	#include <worldpos_vertex>
	#include <clipping_planes_vertex>
	vWorldPosition = worldPosition.xyz;
}`, z_ = `#define DISTANCE
uniform vec3 referencePosition;
uniform float nearDistance;
uniform float farDistance;
varying vec3 vWorldPosition;
#include <common>
#include <uv_pars_fragment>
#include <map_pars_fragment>
#include <alphamap_pars_fragment>
#include <alphatest_pars_fragment>
#include <alphahash_pars_fragment>
#include <clipping_planes_pars_fragment>
void main() {
	vec4 diffuseColor = vec4( 1.0 );
	#include <clipping_planes_fragment>
	#include <map_fragment>
	#include <alphamap_fragment>
	#include <alphatest_fragment>
	#include <alphahash_fragment>
	float dist = length( vWorldPosition - referencePosition );
	dist = ( dist - nearDistance ) / ( farDistance - nearDistance );
	dist = saturate( dist );
	gl_FragColor = vec4( dist, 0.0, 0.0, 1.0 );
}`, G_ = `varying vec3 vWorldDirection;
#include <common>
void main() {
	vWorldDirection = transformDirection( position, modelMatrix );
	#include <begin_vertex>
	#include <project_vertex>
}`, W_ = `uniform sampler2D tEquirect;
varying vec3 vWorldDirection;
#include <common>
void main() {
	vec3 direction = normalize( vWorldDirection );
	vec2 sampleUV = equirectUv( direction );
	gl_FragColor = texture2D( tEquirect, sampleUV );
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
}`, X_ = `uniform float scale;
attribute float lineDistance;
varying float vLineDistance;
#include <common>
#include <uv_pars_vertex>
#include <color_pars_vertex>
#include <fog_pars_vertex>
#include <morphtarget_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>
void main() {
	vLineDistance = scale * lineDistance;
	#include <uv_vertex>
	#include <color_vertex>
	#include <morphinstance_vertex>
	#include <morphcolor_vertex>
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <project_vertex>
	#include <logdepthbuf_vertex>
	#include <clipping_planes_vertex>
	#include <fog_vertex>
}`, j_ = `uniform vec3 diffuse;
uniform float opacity;
uniform float dashSize;
uniform float totalSize;
varying float vLineDistance;
#include <common>
#include <color_pars_fragment>
#include <uv_pars_fragment>
#include <map_pars_fragment>
#include <fog_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>
void main() {
	vec4 diffuseColor = vec4( diffuse, opacity );
	#include <clipping_planes_fragment>
	if ( mod( vLineDistance, totalSize ) > dashSize ) {
		discard;
	}
	vec3 outgoingLight = vec3( 0.0 );
	#include <logdepthbuf_fragment>
	#include <map_fragment>
	#include <color_fragment>
	outgoingLight = diffuseColor.rgb;
	#include <opaque_fragment>
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
	#include <fog_fragment>
	#include <premultiplied_alpha_fragment>
}`, q_ = `#include <common>
#include <batching_pars_vertex>
#include <uv_pars_vertex>
#include <envmap_pars_vertex>
#include <color_pars_vertex>
#include <fog_pars_vertex>
#include <morphtarget_pars_vertex>
#include <skinning_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>
void main() {
	#include <uv_vertex>
	#include <color_vertex>
	#include <morphinstance_vertex>
	#include <morphcolor_vertex>
	#include <batching_vertex>
	#if defined ( USE_ENVMAP ) || defined ( USE_SKINNING )
		#include <beginnormal_vertex>
		#include <morphnormal_vertex>
		#include <skinbase_vertex>
		#include <skinnormal_vertex>
		#include <defaultnormal_vertex>
	#endif
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <skinning_vertex>
	#include <project_vertex>
	#include <logdepthbuf_vertex>
	#include <clipping_planes_vertex>
	#include <worldpos_vertex>
	#include <envmap_vertex>
	#include <fog_vertex>
}`, Y_ = `uniform vec3 diffuse;
uniform float opacity;
#ifndef FLAT_SHADED
	varying vec3 vNormal;
#endif
#include <common>
#include <dithering_pars_fragment>
#include <color_pars_fragment>
#include <uv_pars_fragment>
#include <map_pars_fragment>
#include <alphamap_pars_fragment>
#include <alphatest_pars_fragment>
#include <alphahash_pars_fragment>
#include <aomap_pars_fragment>
#include <lightmap_pars_fragment>
#include <envmap_common_pars_fragment>
#include <envmap_pars_fragment>
#include <fog_pars_fragment>
#include <specularmap_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>
void main() {
	vec4 diffuseColor = vec4( diffuse, opacity );
	#include <clipping_planes_fragment>
	#include <logdepthbuf_fragment>
	#include <map_fragment>
	#include <color_fragment>
	#include <alphamap_fragment>
	#include <alphatest_fragment>
	#include <alphahash_fragment>
	#include <specularmap_fragment>
	ReflectedLight reflectedLight = ReflectedLight( vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ) );
	#ifdef USE_LIGHTMAP
		vec4 lightMapTexel = texture2D( lightMap, vLightMapUv );
		reflectedLight.indirectDiffuse += lightMapTexel.rgb * lightMapIntensity * RECIPROCAL_PI;
	#else
		reflectedLight.indirectDiffuse += vec3( 1.0 );
	#endif
	#include <aomap_fragment>
	reflectedLight.indirectDiffuse *= diffuseColor.rgb;
	vec3 outgoingLight = reflectedLight.indirectDiffuse;
	#include <envmap_fragment>
	#include <opaque_fragment>
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
	#include <fog_fragment>
	#include <premultiplied_alpha_fragment>
	#include <dithering_fragment>
}`, K_ = `#define LAMBERT
varying vec3 vViewPosition;
#include <common>
#include <batching_pars_vertex>
#include <uv_pars_vertex>
#include <displacementmap_pars_vertex>
#include <envmap_pars_vertex>
#include <color_pars_vertex>
#include <fog_pars_vertex>
#include <normal_pars_vertex>
#include <morphtarget_pars_vertex>
#include <skinning_pars_vertex>
#include <shadowmap_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>
void main() {
	#include <uv_vertex>
	#include <color_vertex>
	#include <morphinstance_vertex>
	#include <morphcolor_vertex>
	#include <batching_vertex>
	#include <beginnormal_vertex>
	#include <morphnormal_vertex>
	#include <skinbase_vertex>
	#include <skinnormal_vertex>
	#include <defaultnormal_vertex>
	#include <normal_vertex>
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <skinning_vertex>
	#include <displacementmap_vertex>
	#include <project_vertex>
	#include <logdepthbuf_vertex>
	#include <clipping_planes_vertex>
	vViewPosition = - mvPosition.xyz;
	#include <worldpos_vertex>
	#include <envmap_vertex>
	#include <shadowmap_vertex>
	#include <fog_vertex>
}`, Z_ = `#define LAMBERT
uniform vec3 diffuse;
uniform vec3 emissive;
uniform float opacity;
#include <common>
#include <dithering_pars_fragment>
#include <color_pars_fragment>
#include <uv_pars_fragment>
#include <map_pars_fragment>
#include <alphamap_pars_fragment>
#include <alphatest_pars_fragment>
#include <alphahash_pars_fragment>
#include <aomap_pars_fragment>
#include <lightmap_pars_fragment>
#include <emissivemap_pars_fragment>
#include <cube_uv_reflection_fragment>
#include <envmap_common_pars_fragment>
#include <envmap_pars_fragment>
#include <envmap_physical_pars_fragment>
#include <fog_pars_fragment>
#include <bsdfs>
#include <lights_pars_begin>
#include <normal_pars_fragment>
#include <lights_lambert_pars_fragment>
#include <shadowmap_pars_fragment>
#include <bumpmap_pars_fragment>
#include <normalmap_pars_fragment>
#include <specularmap_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>
void main() {
	vec4 diffuseColor = vec4( diffuse, opacity );
	#include <clipping_planes_fragment>
	ReflectedLight reflectedLight = ReflectedLight( vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ) );
	vec3 totalEmissiveRadiance = emissive;
	#include <logdepthbuf_fragment>
	#include <map_fragment>
	#include <color_fragment>
	#include <alphamap_fragment>
	#include <alphatest_fragment>
	#include <alphahash_fragment>
	#include <specularmap_fragment>
	#include <normal_fragment_begin>
	#include <normal_fragment_maps>
	#include <emissivemap_fragment>
	#include <lights_lambert_fragment>
	#include <lights_fragment_begin>
	#include <lights_fragment_maps>
	#include <lights_fragment_end>
	#include <aomap_fragment>
	vec3 outgoingLight = reflectedLight.directDiffuse + reflectedLight.indirectDiffuse + totalEmissiveRadiance;
	#include <envmap_fragment>
	#include <opaque_fragment>
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
	#include <fog_fragment>
	#include <premultiplied_alpha_fragment>
	#include <dithering_fragment>
}`, $_ = `#define MATCAP
varying vec3 vViewPosition;
#include <common>
#include <batching_pars_vertex>
#include <uv_pars_vertex>
#include <color_pars_vertex>
#include <displacementmap_pars_vertex>
#include <fog_pars_vertex>
#include <normal_pars_vertex>
#include <morphtarget_pars_vertex>
#include <skinning_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>
void main() {
	#include <uv_vertex>
	#include <color_vertex>
	#include <morphinstance_vertex>
	#include <morphcolor_vertex>
	#include <batching_vertex>
	#include <beginnormal_vertex>
	#include <morphnormal_vertex>
	#include <skinbase_vertex>
	#include <skinnormal_vertex>
	#include <defaultnormal_vertex>
	#include <normal_vertex>
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <skinning_vertex>
	#include <displacementmap_vertex>
	#include <project_vertex>
	#include <logdepthbuf_vertex>
	#include <clipping_planes_vertex>
	#include <fog_vertex>
	vViewPosition = - mvPosition.xyz;
}`, Q_ = `#define MATCAP
uniform vec3 diffuse;
uniform float opacity;
uniform sampler2D matcap;
varying vec3 vViewPosition;
#include <common>
#include <dithering_pars_fragment>
#include <color_pars_fragment>
#include <uv_pars_fragment>
#include <map_pars_fragment>
#include <alphamap_pars_fragment>
#include <alphatest_pars_fragment>
#include <alphahash_pars_fragment>
#include <fog_pars_fragment>
#include <normal_pars_fragment>
#include <bumpmap_pars_fragment>
#include <normalmap_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>
void main() {
	vec4 diffuseColor = vec4( diffuse, opacity );
	#include <clipping_planes_fragment>
	#include <logdepthbuf_fragment>
	#include <map_fragment>
	#include <color_fragment>
	#include <alphamap_fragment>
	#include <alphatest_fragment>
	#include <alphahash_fragment>
	#include <normal_fragment_begin>
	#include <normal_fragment_maps>
	vec3 viewDir = normalize( vViewPosition );
	vec3 x = normalize( vec3( viewDir.z, 0.0, - viewDir.x ) );
	vec3 y = cross( viewDir, x );
	vec2 uv = vec2( dot( x, normal ), dot( y, normal ) ) * 0.495 + 0.5;
	#ifdef USE_MATCAP
		vec4 matcapColor = texture2D( matcap, uv );
	#else
		vec4 matcapColor = vec4( vec3( mix( 0.2, 0.8, uv.y ) ), 1.0 );
	#endif
	vec3 outgoingLight = diffuseColor.rgb * matcapColor.rgb;
	#include <opaque_fragment>
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
	#include <fog_fragment>
	#include <premultiplied_alpha_fragment>
	#include <dithering_fragment>
}`, J_ = `#define NORMAL
#if defined( FLAT_SHADED ) || defined( USE_BUMPMAP ) || defined( USE_NORMALMAP_TANGENTSPACE )
	varying vec3 vViewPosition;
#endif
#include <common>
#include <batching_pars_vertex>
#include <uv_pars_vertex>
#include <displacementmap_pars_vertex>
#include <normal_pars_vertex>
#include <morphtarget_pars_vertex>
#include <skinning_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>
void main() {
	#include <uv_vertex>
	#include <batching_vertex>
	#include <beginnormal_vertex>
	#include <morphinstance_vertex>
	#include <morphnormal_vertex>
	#include <skinbase_vertex>
	#include <skinnormal_vertex>
	#include <defaultnormal_vertex>
	#include <normal_vertex>
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <skinning_vertex>
	#include <displacementmap_vertex>
	#include <project_vertex>
	#include <logdepthbuf_vertex>
	#include <clipping_planes_vertex>
#if defined( FLAT_SHADED ) || defined( USE_BUMPMAP ) || defined( USE_NORMALMAP_TANGENTSPACE )
	vViewPosition = - mvPosition.xyz;
#endif
}`, ev = `#define NORMAL
uniform float opacity;
#if defined( FLAT_SHADED ) || defined( USE_BUMPMAP ) || defined( USE_NORMALMAP_TANGENTSPACE )
	varying vec3 vViewPosition;
#endif
#include <uv_pars_fragment>
#include <normal_pars_fragment>
#include <bumpmap_pars_fragment>
#include <normalmap_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>
void main() {
	vec4 diffuseColor = vec4( 0.0, 0.0, 0.0, opacity );
	#include <clipping_planes_fragment>
	#include <logdepthbuf_fragment>
	#include <normal_fragment_begin>
	#include <normal_fragment_maps>
	gl_FragColor = vec4( normalize( normal ) * 0.5 + 0.5, diffuseColor.a );
	#ifdef OPAQUE
		gl_FragColor.a = 1.0;
	#endif
}`, tv = `#define PHONG
varying vec3 vViewPosition;
#include <common>
#include <batching_pars_vertex>
#include <uv_pars_vertex>
#include <displacementmap_pars_vertex>
#include <envmap_pars_vertex>
#include <color_pars_vertex>
#include <fog_pars_vertex>
#include <normal_pars_vertex>
#include <morphtarget_pars_vertex>
#include <skinning_pars_vertex>
#include <shadowmap_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>
void main() {
	#include <uv_vertex>
	#include <color_vertex>
	#include <morphcolor_vertex>
	#include <batching_vertex>
	#include <beginnormal_vertex>
	#include <morphinstance_vertex>
	#include <morphnormal_vertex>
	#include <skinbase_vertex>
	#include <skinnormal_vertex>
	#include <defaultnormal_vertex>
	#include <normal_vertex>
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <skinning_vertex>
	#include <displacementmap_vertex>
	#include <project_vertex>
	#include <logdepthbuf_vertex>
	#include <clipping_planes_vertex>
	vViewPosition = - mvPosition.xyz;
	#include <worldpos_vertex>
	#include <envmap_vertex>
	#include <shadowmap_vertex>
	#include <fog_vertex>
}`, iv = `#define PHONG
uniform vec3 diffuse;
uniform vec3 emissive;
uniform vec3 specular;
uniform float shininess;
uniform float opacity;
#include <common>
#include <dithering_pars_fragment>
#include <color_pars_fragment>
#include <uv_pars_fragment>
#include <map_pars_fragment>
#include <alphamap_pars_fragment>
#include <alphatest_pars_fragment>
#include <alphahash_pars_fragment>
#include <aomap_pars_fragment>
#include <lightmap_pars_fragment>
#include <emissivemap_pars_fragment>
#include <cube_uv_reflection_fragment>
#include <envmap_common_pars_fragment>
#include <envmap_pars_fragment>
#include <envmap_physical_pars_fragment>
#include <fog_pars_fragment>
#include <bsdfs>
#include <lights_pars_begin>
#include <normal_pars_fragment>
#include <lights_phong_pars_fragment>
#include <shadowmap_pars_fragment>
#include <bumpmap_pars_fragment>
#include <normalmap_pars_fragment>
#include <specularmap_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>
void main() {
	vec4 diffuseColor = vec4( diffuse, opacity );
	#include <clipping_planes_fragment>
	ReflectedLight reflectedLight = ReflectedLight( vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ) );
	vec3 totalEmissiveRadiance = emissive;
	#include <logdepthbuf_fragment>
	#include <map_fragment>
	#include <color_fragment>
	#include <alphamap_fragment>
	#include <alphatest_fragment>
	#include <alphahash_fragment>
	#include <specularmap_fragment>
	#include <normal_fragment_begin>
	#include <normal_fragment_maps>
	#include <emissivemap_fragment>
	#include <lights_phong_fragment>
	#include <lights_fragment_begin>
	#include <lights_fragment_maps>
	#include <lights_fragment_end>
	#include <aomap_fragment>
	vec3 outgoingLight = reflectedLight.directDiffuse + reflectedLight.indirectDiffuse + reflectedLight.directSpecular + reflectedLight.indirectSpecular + totalEmissiveRadiance;
	#include <envmap_fragment>
	#include <opaque_fragment>
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
	#include <fog_fragment>
	#include <premultiplied_alpha_fragment>
	#include <dithering_fragment>
}`, rv = `#define STANDARD
varying vec3 vViewPosition;
#ifdef USE_TRANSMISSION
	varying vec3 vWorldPosition;
#endif
#include <common>
#include <batching_pars_vertex>
#include <uv_pars_vertex>
#include <displacementmap_pars_vertex>
#include <color_pars_vertex>
#include <fog_pars_vertex>
#include <normal_pars_vertex>
#include <morphtarget_pars_vertex>
#include <skinning_pars_vertex>
#include <shadowmap_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>
void main() {
	#include <uv_vertex>
	#include <color_vertex>
	#include <morphinstance_vertex>
	#include <morphcolor_vertex>
	#include <batching_vertex>
	#include <beginnormal_vertex>
	#include <morphnormal_vertex>
	#include <skinbase_vertex>
	#include <skinnormal_vertex>
	#include <defaultnormal_vertex>
	#include <normal_vertex>
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <skinning_vertex>
	#include <displacementmap_vertex>
	#include <project_vertex>
	#include <logdepthbuf_vertex>
	#include <clipping_planes_vertex>
	vViewPosition = - mvPosition.xyz;
	#include <worldpos_vertex>
	#include <shadowmap_vertex>
	#include <fog_vertex>
#ifdef USE_TRANSMISSION
	vWorldPosition = worldPosition.xyz;
#endif
}`, nv = `#define STANDARD
#ifdef PHYSICAL
	#define IOR
	#define USE_SPECULAR
#endif
uniform vec3 diffuse;
uniform vec3 emissive;
uniform float roughness;
uniform float metalness;
uniform float opacity;
#ifdef IOR
	uniform float ior;
#endif
#ifdef USE_SPECULAR
	uniform float specularIntensity;
	uniform vec3 specularColor;
	#ifdef USE_SPECULAR_COLORMAP
		uniform sampler2D specularColorMap;
	#endif
	#ifdef USE_SPECULAR_INTENSITYMAP
		uniform sampler2D specularIntensityMap;
	#endif
#endif
#ifdef USE_CLEARCOAT
	uniform float clearcoat;
	uniform float clearcoatRoughness;
#endif
#ifdef USE_DISPERSION
	uniform float dispersion;
#endif
#ifdef USE_RETROREFLECTION
	uniform float retroreflectivity;
#endif
#ifdef USE_IRIDESCENCE
	uniform float iridescence;
	uniform float iridescenceIOR;
	uniform float iridescenceThicknessMinimum;
	uniform float iridescenceThicknessMaximum;
#endif
#ifdef USE_SHEEN
	uniform vec3 sheenColor;
	uniform float sheenRoughness;
	#ifdef USE_SHEEN_COLORMAP
		uniform sampler2D sheenColorMap;
	#endif
	#ifdef USE_SHEEN_ROUGHNESSMAP
		uniform sampler2D sheenRoughnessMap;
	#endif
#endif
#ifdef USE_ANISOTROPY
	uniform vec2 anisotropyVector;
	#ifdef USE_ANISOTROPYMAP
		uniform sampler2D anisotropyMap;
	#endif
#endif
varying vec3 vViewPosition;
#include <common>
#include <dithering_pars_fragment>
#include <color_pars_fragment>
#include <uv_pars_fragment>
#include <map_pars_fragment>
#include <alphamap_pars_fragment>
#include <alphatest_pars_fragment>
#include <alphahash_pars_fragment>
#include <aomap_pars_fragment>
#include <lightmap_pars_fragment>
#include <emissivemap_pars_fragment>
#include <iridescence_fragment>
#include <cube_uv_reflection_fragment>
#include <envmap_common_pars_fragment>
#include <envmap_physical_pars_fragment>
#include <fog_pars_fragment>
#include <lights_pars_begin>
#include <normal_pars_fragment>
#include <lights_physical_pars_fragment>
#include <transmission_pars_fragment>
#include <shadowmap_pars_fragment>
#include <bumpmap_pars_fragment>
#include <normalmap_pars_fragment>
#include <clearcoat_pars_fragment>
#include <iridescence_pars_fragment>
#include <roughnessmap_pars_fragment>
#include <metalnessmap_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>
void main() {
	vec4 diffuseColor = vec4( diffuse, opacity );
	#include <clipping_planes_fragment>
	ReflectedLight reflectedLight = ReflectedLight( vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ) );
	vec3 totalEmissiveRadiance = emissive;
	#include <logdepthbuf_fragment>
	#include <map_fragment>
	#include <color_fragment>
	#include <alphamap_fragment>
	#include <alphatest_fragment>
	#include <alphahash_fragment>
	#include <roughnessmap_fragment>
	#include <metalnessmap_fragment>
	#include <normal_fragment_begin>
	#include <normal_fragment_maps>
	#include <clearcoat_normal_fragment_begin>
	#include <clearcoat_normal_fragment_maps>
	#include <emissivemap_fragment>
	#include <lights_physical_fragment>
	#include <lights_fragment_begin>
	#include <lights_fragment_maps>
	#include <lights_fragment_end>
	#include <aomap_fragment>
	vec3 totalDiffuse = reflectedLight.directDiffuse + reflectedLight.indirectDiffuse;
	vec3 totalSpecular = reflectedLight.directSpecular + reflectedLight.indirectSpecular;
	#include <transmission_fragment>
	vec3 outgoingLight = totalDiffuse + totalSpecular + totalEmissiveRadiance;
	#ifdef USE_SHEEN
 
		outgoingLight = outgoingLight + sheenSpecularDirect + sheenSpecularIndirect;
 
 	#endif
	#ifdef USE_CLEARCOAT
		float dotNVcc = saturate( dot( geometryClearcoatNormal, geometryViewDir ) );
		vec3 Fcc = F_Schlick( material.clearcoatF0, material.clearcoatF90, dotNVcc );
		outgoingLight = outgoingLight * ( 1.0 - material.clearcoat * Fcc ) + ( clearcoatSpecularDirect + clearcoatSpecularIndirect ) * material.clearcoat;
	#endif
	#include <opaque_fragment>
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
	#include <fog_fragment>
	#include <premultiplied_alpha_fragment>
	#include <dithering_fragment>
}`, sv = `#define TOON
varying vec3 vViewPosition;
#include <common>
#include <batching_pars_vertex>
#include <uv_pars_vertex>
#include <displacementmap_pars_vertex>
#include <color_pars_vertex>
#include <fog_pars_vertex>
#include <normal_pars_vertex>
#include <morphtarget_pars_vertex>
#include <skinning_pars_vertex>
#include <shadowmap_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>
void main() {
	#include <uv_vertex>
	#include <color_vertex>
	#include <morphinstance_vertex>
	#include <morphcolor_vertex>
	#include <batching_vertex>
	#include <beginnormal_vertex>
	#include <morphnormal_vertex>
	#include <skinbase_vertex>
	#include <skinnormal_vertex>
	#include <defaultnormal_vertex>
	#include <normal_vertex>
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <skinning_vertex>
	#include <displacementmap_vertex>
	#include <project_vertex>
	#include <logdepthbuf_vertex>
	#include <clipping_planes_vertex>
	vViewPosition = - mvPosition.xyz;
	#include <worldpos_vertex>
	#include <shadowmap_vertex>
	#include <fog_vertex>
}`, av = `#define TOON
uniform vec3 diffuse;
uniform vec3 emissive;
uniform float opacity;
#include <common>
#include <dithering_pars_fragment>
#include <color_pars_fragment>
#include <uv_pars_fragment>
#include <map_pars_fragment>
#include <alphamap_pars_fragment>
#include <alphatest_pars_fragment>
#include <alphahash_pars_fragment>
#include <aomap_pars_fragment>
#include <lightmap_pars_fragment>
#include <emissivemap_pars_fragment>
#include <gradientmap_pars_fragment>
#include <fog_pars_fragment>
#include <bsdfs>
#include <lights_pars_begin>
#include <normal_pars_fragment>
#include <lights_toon_pars_fragment>
#include <shadowmap_pars_fragment>
#include <bumpmap_pars_fragment>
#include <normalmap_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>
void main() {
	vec4 diffuseColor = vec4( diffuse, opacity );
	#include <clipping_planes_fragment>
	ReflectedLight reflectedLight = ReflectedLight( vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ) );
	vec3 totalEmissiveRadiance = emissive;
	#include <logdepthbuf_fragment>
	#include <map_fragment>
	#include <color_fragment>
	#include <alphamap_fragment>
	#include <alphatest_fragment>
	#include <alphahash_fragment>
	#include <normal_fragment_begin>
	#include <normal_fragment_maps>
	#include <emissivemap_fragment>
	#include <lights_toon_fragment>
	#include <lights_fragment_begin>
	#include <lights_fragment_maps>
	#include <lights_fragment_end>
	#include <aomap_fragment>
	vec3 outgoingLight = reflectedLight.directDiffuse + reflectedLight.indirectDiffuse + totalEmissiveRadiance;
	#include <opaque_fragment>
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
	#include <fog_fragment>
	#include <premultiplied_alpha_fragment>
	#include <dithering_fragment>
}`, ov = `uniform float size;
uniform float scale;
#include <common>
#include <color_pars_vertex>
#include <fog_pars_vertex>
#include <morphtarget_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>
#ifdef USE_POINTS_UV
	varying vec2 vUv;
	uniform mat3 uvTransform;
#endif
void main() {
	#ifdef USE_POINTS_UV
		vUv = ( uvTransform * vec3( uv, 1 ) ).xy;
	#endif
	#include <color_vertex>
	#include <morphinstance_vertex>
	#include <morphcolor_vertex>
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <project_vertex>
	gl_PointSize = size;
	#ifdef USE_SIZEATTENUATION
		bool isPerspective = isPerspectiveMatrix( projectionMatrix );
		if ( isPerspective ) gl_PointSize *= ( scale / - mvPosition.z );
	#endif
	#include <logdepthbuf_vertex>
	#include <clipping_planes_vertex>
	#include <worldpos_vertex>
	#include <fog_vertex>
}`, lv = `uniform vec3 diffuse;
uniform float opacity;
#include <common>
#include <color_pars_fragment>
#include <map_particle_pars_fragment>
#include <alphatest_pars_fragment>
#include <alphahash_pars_fragment>
#include <fog_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>
void main() {
	vec4 diffuseColor = vec4( diffuse, opacity );
	#include <clipping_planes_fragment>
	vec3 outgoingLight = vec3( 0.0 );
	#include <logdepthbuf_fragment>
	#include <map_particle_fragment>
	#include <color_fragment>
	#include <alphatest_fragment>
	#include <alphahash_fragment>
	outgoingLight = diffuseColor.rgb;
	#include <opaque_fragment>
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
	#include <fog_fragment>
	#include <premultiplied_alpha_fragment>
}`, cv = `#include <common>
#include <batching_pars_vertex>
#include <fog_pars_vertex>
#include <morphtarget_pars_vertex>
#include <skinning_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <shadowmap_pars_vertex>
void main() {
	#include <batching_vertex>
	#include <beginnormal_vertex>
	#include <morphinstance_vertex>
	#include <morphnormal_vertex>
	#include <skinbase_vertex>
	#include <skinnormal_vertex>
	#include <defaultnormal_vertex>
	#include <begin_vertex>
	#include <morphtarget_vertex>
	#include <skinning_vertex>
	#include <project_vertex>
	#include <logdepthbuf_vertex>
	#include <worldpos_vertex>
	#include <shadowmap_vertex>
	#include <fog_vertex>
}`, hv = `uniform vec3 color;
uniform float opacity;
#include <common>
#include <fog_pars_fragment>
#include <bsdfs>
#include <lights_pars_begin>
#include <logdepthbuf_pars_fragment>
#include <shadowmap_pars_fragment>
#include <shadowmask_pars_fragment>
void main() {
	#include <logdepthbuf_fragment>
	gl_FragColor = vec4( color, opacity * ( 1.0 - getShadowMask() ) );
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
	#include <fog_fragment>
	#include <premultiplied_alpha_fragment>
}`, uv = `uniform float rotation;
uniform vec2 center;
#include <common>
#include <uv_pars_vertex>
#include <fog_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>
void main() {
	#include <uv_vertex>
	vec4 mvPosition = modelViewMatrix[ 3 ];
	vec2 scale = vec2( length( modelMatrix[ 0 ].xyz ), length( modelMatrix[ 1 ].xyz ) );
	#ifndef USE_SIZEATTENUATION
		bool isPerspective = isPerspectiveMatrix( projectionMatrix );
		if ( isPerspective ) scale *= - mvPosition.z;
	#endif
	vec2 alignedPosition = ( position.xy - ( center - vec2( 0.5 ) ) ) * scale;
	vec2 rotatedPosition;
	rotatedPosition.x = cos( rotation ) * alignedPosition.x - sin( rotation ) * alignedPosition.y;
	rotatedPosition.y = sin( rotation ) * alignedPosition.x + cos( rotation ) * alignedPosition.y;
	mvPosition.xy += rotatedPosition;
	gl_Position = projectionMatrix * mvPosition;
	#include <logdepthbuf_vertex>
	#include <clipping_planes_vertex>
	#include <fog_vertex>
}`, dv = `uniform vec3 diffuse;
uniform float opacity;
#include <common>
#include <uv_pars_fragment>
#include <map_pars_fragment>
#include <alphamap_pars_fragment>
#include <alphatest_pars_fragment>
#include <alphahash_pars_fragment>
#include <fog_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>
void main() {
	vec4 diffuseColor = vec4( diffuse, opacity );
	#include <clipping_planes_fragment>
	vec3 outgoingLight = vec3( 0.0 );
	#include <logdepthbuf_fragment>
	#include <map_fragment>
	#include <alphamap_fragment>
	#include <alphatest_fragment>
	#include <alphahash_fragment>
	outgoingLight = diffuseColor.rgb;
	#include <opaque_fragment>
	#include <tonemapping_fragment>
	#include <colorspace_fragment>
	#include <fog_fragment>
}`, Ge = { alphahash_fragment: Nm, alphahash_pars_fragment: Um, alphamap_fragment: Dm, alphamap_pars_fragment: Om, alphatest_fragment: Fm, alphatest_pars_fragment: Bm, aomap_fragment: Vm, aomap_pars_fragment: Hm, batching_pars_vertex: km, batching_vertex: zm, begin_vertex: Gm, beginnormal_vertex: Wm, bsdfs: Xm, iridescence_fragment: jm, bumpmap_pars_fragment: qm, clipping_planes_fragment: Ym, clipping_planes_pars_fragment: Km, clipping_planes_pars_vertex: Zm, clipping_planes_vertex: $m, color_fragment: Qm, color_pars_fragment: Jm, color_pars_vertex: eg, color_vertex: tg, common: ig, cube_uv_reflection_fragment: rg, defaultnormal_vertex: ng, displacementmap_pars_vertex: sg, displacementmap_vertex: ag, emissivemap_fragment: og, emissivemap_pars_fragment: lg, colorspace_fragment: cg, colorspace_pars_fragment: hg, envmap_fragment: ug, envmap_common_pars_fragment: dg, envmap_pars_fragment: pg, envmap_pars_vertex: fg, envmap_physical_pars_fragment: bg, envmap_vertex: mg, fog_vertex: gg, fog_pars_vertex: _g, fog_fragment: vg, fog_pars_fragment: xg, gradientmap_pars_fragment: Mg, lightmap_pars_fragment: yg, lights_lambert_fragment: Sg, lights_lambert_pars_fragment: Tg, lights_pars_begin: Eg, lights_toon_fragment: wg, lights_toon_pars_fragment: Ag, lights_phong_fragment: Rg, lights_phong_pars_fragment: Pg, lights_physical_fragment: Cg, lights_physical_pars_fragment: Lg, lights_fragment_begin: Ig, lights_fragment_maps: Ng, lights_fragment_end: Ug, lightprobes_pars_fragment: Dg, logdepthbuf_fragment: Og, logdepthbuf_pars_fragment: Fg, logdepthbuf_pars_vertex: Bg, logdepthbuf_vertex: Vg, map_fragment: Hg, map_pars_fragment: kg, map_particle_fragment: zg, map_particle_pars_fragment: Gg, metalnessmap_fragment: Wg, metalnessmap_pars_fragment: Xg, morphinstance_vertex: jg, morphcolor_vertex: qg, morphnormal_vertex: Yg, morphtarget_pars_vertex: Kg, morphtarget_vertex: Zg, normal_fragment_begin: $g, normal_fragment_maps: Qg, normal_pars_fragment: Jg, normal_pars_vertex: e_, normal_vertex: t_, normalmap_pars_fragment: i_, clearcoat_normal_fragment_begin: r_, clearcoat_normal_fragment_maps: n_, clearcoat_pars_fragment: s_, iridescence_pars_fragment: a_, opaque_fragment: o_, packing: l_, premultiplied_alpha_fragment: c_, project_vertex: h_, dithering_fragment: u_, dithering_pars_fragment: d_, roughnessmap_fragment: p_, roughnessmap_pars_fragment: f_, shadowmap_pars_fragment: m_, shadowmap_pars_vertex: g_, shadowmap_vertex: __, shadowmask_pars_fragment: v_, skinbase_vertex: x_, skinning_pars_vertex: M_, skinning_vertex: y_, skinnormal_vertex: S_, specularmap_fragment: T_, specularmap_pars_fragment: E_, tonemapping_fragment: b_, tonemapping_pars_fragment: w_, transmission_fragment: A_, transmission_pars_fragment: R_, uv_pars_fragment: P_, uv_pars_vertex: C_, uv_vertex: L_, worldpos_vertex: I_, background_vert: N_, background_frag: U_, backgroundCube_vert: D_, backgroundCube_frag: O_, cube_vert: F_, cube_frag: B_, depth_vert: V_, depth_frag: H_, distance_vert: k_, distance_frag: z_, equirect_vert: G_, equirect_frag: W_, linedashed_vert: X_, linedashed_frag: j_, meshbasic_vert: q_, meshbasic_frag: Y_, meshlambert_vert: K_, meshlambert_frag: Z_, meshmatcap_vert: $_, meshmatcap_frag: Q_, meshnormal_vert: J_, meshnormal_frag: ev, meshphong_vert: tv, meshphong_frag: iv, meshphysical_vert: rv, meshphysical_frag: nv, meshtoon_vert: sv, meshtoon_frag: av, points_vert: ov, points_frag: lv, shadow_vert: cv, shadow_frag: hv, sprite_vert: uv, sprite_frag: dv }, fe = { common: { diffuse: { value: new be(16777215) }, opacity: { value: 1 }, map: { value: null }, mapTransform: { value: new De() }, alphaMap: { value: null }, alphaMapTransform: { value: new De() }, alphaTest: { value: 0 } }, specularmap: { specularMap: { value: null }, specularMapTransform: { value: new De() } }, envmap: { envMap: { value: null }, envMapRotation: { value: new De() }, reflectivity: { value: 1 }, ior: { value: 1.5 }, refractionRatio: { value: 0.98 }, dfgLUT: { value: null } }, aomap: { aoMap: { value: null }, aoMapIntensity: { value: 1 }, aoMapTransform: { value: new De() } }, lightmap: { lightMap: { value: null }, lightMapIntensity: { value: 1 }, lightMapTransform: { value: new De() } }, bumpmap: { bumpMap: { value: null }, bumpMapTransform: { value: new De() }, bumpScale: { value: 1 } }, normalmap: { normalMap: { value: null }, normalMapTransform: { value: new De() }, normalScale: { value: new We(1, 1) } }, displacementmap: { displacementMap: { value: null }, displacementMapTransform: { value: new De() }, displacementScale: { value: 1 }, displacementBias: { value: 0 } }, emissivemap: { emissiveMap: { value: null }, emissiveMapTransform: { value: new De() } }, metalnessmap: { metalnessMap: { value: null }, metalnessMapTransform: { value: new De() } }, roughnessmap: { roughnessMap: { value: null }, roughnessMapTransform: { value: new De() } }, gradientmap: { gradientMap: { value: null } }, fog: { fogDensity: { value: 25e-5 }, fogNear: { value: 1 }, fogFar: { value: 2e3 }, fogColor: { value: new be(16777215) } }, lights: { ambientLightColor: { value: [] }, lightProbe: { value: [] }, sunLights: { value: [], properties: { direction: {}, color: {} } }, sunLightShadows: { value: [], properties: { shadowIntensity: 1, shadowBias: {}, shadowNormalBias: {}, shadowRadius: {}, shadowMapSize: {} } }, sunShadowMatrix: { value: [] }, sunShadowCascade: { value: [] }, directionalLights: { value: [], properties: { direction: {}, color: {} } }, directionalLightShadows: { value: [], properties: { shadowIntensity: 1, shadowBias: {}, shadowNormalBias: {}, shadowRadius: {}, shadowMapSize: {} } }, directionalShadowMatrix: { value: [] }, spotLights: { value: [], properties: { color: {}, position: {}, direction: {}, distance: {}, coneCos: {}, penumbraCos: {}, decay: {} } }, spotLightShadows: { value: [], properties: { shadowIntensity: 1, shadowBias: {}, shadowNormalBias: {}, shadowRadius: {}, shadowMapSize: {} } }, spotLightMap: { value: [] }, spotLightMatrix: { value: [] }, pointLights: { value: [], properties: { color: {}, position: {}, decay: {}, distance: {} } }, pointLightShadows: { value: [], properties: { shadowIntensity: 1, shadowBias: {}, shadowNormalBias: {}, shadowRadius: {}, shadowMapSize: {}, shadowCameraNear: {}, shadowCameraFar: {} } }, pointShadowMatrix: { value: [] }, hemisphereLights: { value: [], properties: { direction: {}, skyColor: {}, groundColor: {} } }, rectAreaLights: { value: [], properties: { color: {}, position: {}, width: {}, height: {} } }, ltc_1: { value: null }, ltc_2: { value: null }, probesSH: { value: null }, probesMin: { value: new P() }, probesMax: { value: new P() }, probesResolution: { value: new P() } }, points: { diffuse: { value: new be(16777215) }, opacity: { value: 1 }, size: { value: 1 }, scale: { value: 1 }, map: { value: null }, alphaMap: { value: null }, alphaMapTransform: { value: new De() }, alphaTest: { value: 0 }, uvTransform: { value: new De() } }, sprite: { diffuse: { value: new be(16777215) }, opacity: { value: 1 }, center: { value: new We(0.5, 0.5) }, rotation: { value: 0 }, map: { value: null }, mapTransform: { value: new De() }, alphaMap: { value: null }, alphaMapTransform: { value: new De() }, alphaTest: { value: 0 } } }, Pi = { basic: { uniforms: kt([fe.common, fe.specularmap, fe.envmap, fe.aomap, fe.lightmap, fe.fog]), vertexShader: Ge.meshbasic_vert, fragmentShader: Ge.meshbasic_frag }, lambert: { uniforms: kt([fe.common, fe.specularmap, fe.envmap, fe.aomap, fe.lightmap, fe.emissivemap, fe.bumpmap, fe.normalmap, fe.displacementmap, fe.fog, fe.lights, { emissive: { value: new be(0) }, envMapIntensity: { value: 1 } }]), vertexShader: Ge.meshlambert_vert, fragmentShader: Ge.meshlambert_frag }, phong: { uniforms: kt([fe.common, fe.specularmap, fe.envmap, fe.aomap, fe.lightmap, fe.emissivemap, fe.bumpmap, fe.normalmap, fe.displacementmap, fe.fog, fe.lights, { emissive: { value: new be(0) }, specular: { value: new be(1118481) }, shininess: { value: 30 }, envMapIntensity: { value: 1 } }]), vertexShader: Ge.meshphong_vert, fragmentShader: Ge.meshphong_frag }, standard: { uniforms: kt([fe.common, fe.envmap, fe.aomap, fe.lightmap, fe.emissivemap, fe.bumpmap, fe.normalmap, fe.displacementmap, fe.roughnessmap, fe.metalnessmap, fe.fog, fe.lights, { emissive: { value: new be(0) }, roughness: { value: 1 }, metalness: { value: 0 }, envMapIntensity: { value: 1 } }]), vertexShader: Ge.meshphysical_vert, fragmentShader: Ge.meshphysical_frag }, toon: { uniforms: kt([fe.common, fe.aomap, fe.lightmap, fe.emissivemap, fe.bumpmap, fe.normalmap, fe.displacementmap, fe.gradientmap, fe.fog, fe.lights, { emissive: { value: new be(0) } }]), vertexShader: Ge.meshtoon_vert, fragmentShader: Ge.meshtoon_frag }, matcap: { uniforms: kt([fe.common, fe.bumpmap, fe.normalmap, fe.displacementmap, fe.fog, { matcap: { value: null } }]), vertexShader: Ge.meshmatcap_vert, fragmentShader: Ge.meshmatcap_frag }, points: { uniforms: kt([fe.points, fe.fog]), vertexShader: Ge.points_vert, fragmentShader: Ge.points_frag }, dashed: { uniforms: kt([fe.common, fe.fog, { scale: { value: 1 }, dashSize: { value: 1 }, totalSize: { value: 2 } }]), vertexShader: Ge.linedashed_vert, fragmentShader: Ge.linedashed_frag }, depth: { uniforms: kt([fe.common, fe.displacementmap]), vertexShader: Ge.depth_vert, fragmentShader: Ge.depth_frag }, normal: { uniforms: kt([fe.common, fe.bumpmap, fe.normalmap, fe.displacementmap, { opacity: { value: 1 } }]), vertexShader: Ge.meshnormal_vert, fragmentShader: Ge.meshnormal_frag }, sprite: { uniforms: kt([fe.sprite, fe.fog]), vertexShader: Ge.sprite_vert, fragmentShader: Ge.sprite_frag }, background: { uniforms: { uvTransform: { value: new De() }, t2D: { value: null }, backgroundIntensity: { value: 1 } }, vertexShader: Ge.background_vert, fragmentShader: Ge.background_frag }, backgroundCube: { uniforms: { envMap: { value: null }, backgroundBlurriness: { value: 0 }, backgroundIntensity: { value: 1 }, backgroundRotation: { value: new De() } }, vertexShader: Ge.backgroundCube_vert, fragmentShader: Ge.backgroundCube_frag }, cube: { uniforms: { tCube: { value: null }, tFlip: { value: -1 }, opacity: { value: 1 } }, vertexShader: Ge.cube_vert, fragmentShader: Ge.cube_frag }, equirect: { uniforms: { tEquirect: { value: null } }, vertexShader: Ge.equirect_vert, fragmentShader: Ge.equirect_frag }, distance: { uniforms: kt([fe.common, fe.displacementmap, { referencePosition: { value: new P() }, nearDistance: { value: 1 }, farDistance: { value: 1e3 } }]), vertexShader: Ge.distance_vert, fragmentShader: Ge.distance_frag }, shadow: { uniforms: kt([fe.lights, fe.fog, { color: { value: new be(0) }, opacity: { value: 1 } }]), vertexShader: Ge.shadow_vert, fragmentShader: Ge.shadow_frag } };
Pi.physical = { uniforms: kt([Pi.standard.uniforms, { clearcoat: { value: 0 }, clearcoatMap: { value: null }, clearcoatMapTransform: { value: new De() }, clearcoatNormalMap: { value: null }, clearcoatNormalMapTransform: { value: new De() }, clearcoatNormalScale: { value: new We(1, 1) }, clearcoatRoughness: { value: 0 }, clearcoatRoughnessMap: { value: null }, clearcoatRoughnessMapTransform: { value: new De() }, dispersion: { value: 0 }, retroreflectivity: { value: 0 }, iridescence: { value: 0 }, iridescenceMap: { value: null }, iridescenceMapTransform: { value: new De() }, iridescenceIOR: { value: 1.3 }, iridescenceThicknessMinimum: { value: 100 }, iridescenceThicknessMaximum: { value: 400 }, iridescenceThicknessMap: { value: null }, iridescenceThicknessMapTransform: { value: new De() }, sheen: { value: 0 }, sheenColor: { value: new be(0) }, sheenColorMap: { value: null }, sheenColorMapTransform: { value: new De() }, sheenRoughness: { value: 1 }, sheenRoughnessMap: { value: null }, sheenRoughnessMapTransform: { value: new De() }, transmission: { value: 0 }, transmissionMap: { value: null }, transmissionMapTransform: { value: new De() }, transmissionSamplerSize: { value: new We() }, transmissionSamplerMap: { value: null }, thickness: { value: 0 }, thicknessMap: { value: null }, thicknessMapTransform: { value: new De() }, attenuationDistance: { value: 0 }, attenuationColor: { value: new be(0) }, specularColor: { value: new be(1, 1, 1) }, specularColorMap: { value: null }, specularColorMapTransform: { value: new De() }, specularIntensity: { value: 1 }, specularIntensityMap: { value: null }, specularIntensityMapTransform: { value: new De() }, anisotropyVector: { value: new We() }, anisotropyMap: { value: null }, anisotropyMapTransform: { value: new De() } }]), vertexShader: Ge.meshphysical_vert, fragmentShader: Ge.meshphysical_frag };
const ks = { r: 0, b: 0, g: 0 }, pv = new Fe(), fd = new De();
fd.set(-1, 0, 0, 0, 1, 0, 0, 0, 1);
function fv(r, e, t, i, n, s) {
  const a = new be(0);
  let o = n === !0 ? 0 : 1, l, c, h = null, d = 0, u = null;
  function p(E) {
    let R = E.isScene === !0 ? E.background : null;
    if (R && R.isTexture) {
      const y = E.backgroundBlurriness > 0;
      R = e.get(R, y);
    }
    return R;
  }
  function f(E) {
    let R = !1;
    const y = p(E);
    y === null ? m(a, o) : y && y.isColor && (m(y, 1), R = !0);
    const S = r.xr.getEnvironmentBlendMode();
    S === "additive" ? t.buffers.color.setClear(0, 0, 0, 1, s) : S === "alpha-blend" && t.buffers.color.setClear(0, 0, 0, 0, s), (r.autoClear || R) && (t.buffers.depth.setTest(!0), t.buffers.depth.setMask(!0), t.buffers.color.setMask(!0), r.clear(r.autoClearColor, r.autoClearDepth, r.autoClearStencil));
  }
  function v(E, R) {
    const y = p(R);
    y && (y.isCubeTexture || y.mapping === xa) ? (c === void 0 && (c = new Wt(new ps(1, 1, 1), new xi({ name: "BackgroundCubeMaterial", uniforms: xn(Pi.backgroundCube.uniforms), vertexShader: Pi.backgroundCube.vertexShader, fragmentShader: Pi.backgroundCube.fragmentShader, side: Gt, depthTest: !1, depthWrite: !1, fog: !1, allowOverride: !1 })), c.geometry.deleteAttribute("normal"), c.geometry.deleteAttribute("uv"), c.onBeforeRender = function(S, T, A) {
      this.matrixWorld.copyPosition(A.matrixWorld);
    }, Object.defineProperty(c.material, "envMap", { get: function() {
      return this.uniforms.envMap.value;
    } }), i.update(c)), c.material.uniforms.envMap.value = y, c.material.uniforms.backgroundBlurriness.value = R.backgroundBlurriness, c.material.uniforms.backgroundIntensity.value = R.backgroundIntensity, c.material.uniforms.backgroundRotation.value.setFromMatrix4(pv.makeRotationFromEuler(R.backgroundRotation)).transpose(), y.isCubeTexture && y.isRenderTargetTexture === !1 && c.material.uniforms.backgroundRotation.value.premultiply(fd), c.material.toneMapped = Ze.getTransfer(y.colorSpace) !== ot, (h !== y || d !== y.version || u !== r.toneMapping) && (c.material.needsUpdate = !0, h = y, d = y.version, u = r.toneMapping), c.layers.enableAll(), E.unshift(c, c.geometry, c.material, 0, 0, null)) : y && y.isTexture && (l === void 0 && (l = new Wt(new Sa(2, 2), new xi({ name: "BackgroundMaterial", uniforms: xn(Pi.background.uniforms), vertexShader: Pi.background.vertexShader, fragmentShader: Pi.background.fragmentShader, side: fr, depthTest: !1, depthWrite: !1, fog: !1, allowOverride: !1 })), l.geometry.deleteAttribute("normal"), Object.defineProperty(l.material, "map", { get: function() {
      return this.uniforms.t2D.value;
    } }), i.update(l)), l.material.uniforms.t2D.value = y, l.material.uniforms.backgroundIntensity.value = R.backgroundIntensity, l.material.toneMapped = Ze.getTransfer(y.colorSpace) !== ot, y.matrixAutoUpdate === !0 && y.updateMatrix(), l.material.uniforms.uvTransform.value.copy(y.matrix), (h !== y || d !== y.version || u !== r.toneMapping) && (l.material.needsUpdate = !0, h = y, d = y.version, u = r.toneMapping), l.layers.enableAll(), E.unshift(l, l.geometry, l.material, 0, 0, null));
  }
  function m(E, R) {
    E.getRGB(ks, nd(r)), t.buffers.color.setClear(ks.r, ks.g, ks.b, R, s);
  }
  function g() {
    c !== void 0 && (c.geometry.dispose(), c.material.dispose(), c = void 0), l !== void 0 && (l.geometry.dispose(), l.material.dispose(), l = void 0);
  }
  return { getClearColor: function() {
    return a;
  }, setClearColor: function(E, R = 1) {
    a.set(E), o = R, m(a, o);
  }, getClearAlpha: function() {
    return o;
  }, setClearAlpha: function(E) {
    o = E, m(a, o);
  }, render: f, addToRenderList: v, dispose: g };
}
function mv(r, e) {
  const t = r.getParameter(r.MAX_VERTEX_ATTRIBS), i = {}, n = u(null);
  let s = n, a = !1;
  function o(O, k, q, B, G) {
    let Z = !1;
    const X = d(O, B, q, k);
    s !== X && (s = X, c(s.object)), Z = p(O, B, q, G), Z && f(O, B, q, G), G !== null && e.update(G, r.ELEMENT_ARRAY_BUFFER), (Z || a) && (a = !1, y(O, k, q, B), G !== null && r.bindBuffer(r.ELEMENT_ARRAY_BUFFER, e.get(G).buffer));
  }
  function l() {
    return r.createVertexArray();
  }
  function c(O) {
    return r.bindVertexArray(O);
  }
  function h(O) {
    return r.deleteVertexArray(O);
  }
  function d(O, k, q, B) {
    const G = B.wireframe === !0;
    let Z = i[k.id];
    Z === void 0 && (Z = {}, i[k.id] = Z);
    const X = O.isInstancedMesh === !0 ? O.id : 0;
    let te = Z[X];
    te === void 0 && (te = {}, Z[X] = te);
    let V = te[q.id];
    V === void 0 && (V = {}, te[q.id] = V);
    let K = V[G];
    return K === void 0 && (K = u(l()), V[G] = K), K;
  }
  function u(O) {
    const k = [], q = [], B = [];
    for (let G = 0; G < t; G++) k[G] = 0, q[G] = 0, B[G] = 0;
    return { geometry: null, program: null, wireframe: !1, newAttributes: k, enabledAttributes: q, attributeDivisors: B, object: O, attributes: {}, index: null };
  }
  function p(O, k, q, B) {
    const G = s.attributes, Z = k.attributes;
    let X = 0;
    const te = q.getAttributes();
    for (const V in te) if (te[V].location >= 0) {
      const K = G[V];
      let ee = Z[V];
      if (ee === void 0 && (V === "instanceMatrix" && O.instanceMatrix && (ee = O.instanceMatrix), V === "instanceColor" && O.instanceColor && (ee = O.instanceColor)), K === void 0 || K.attribute !== ee || ee && K.data !== ee.data) return !0;
      X++;
    }
    return s.attributesNum !== X || s.index !== B;
  }
  function f(O, k, q, B) {
    const G = {}, Z = k.attributes;
    let X = 0;
    const te = q.getAttributes();
    for (const V in te) if (te[V].location >= 0) {
      let K = Z[V];
      K === void 0 && (V === "instanceMatrix" && O.instanceMatrix && (K = O.instanceMatrix), V === "instanceColor" && O.instanceColor && (K = O.instanceColor));
      const ee = {};
      ee.attribute = K, K && K.data && (ee.data = K.data), G[V] = ee, X++;
    }
    s.attributes = G, s.attributesNum = X, s.index = B;
  }
  function v() {
    const O = s.newAttributes;
    for (let k = 0, q = O.length; k < q; k++) O[k] = 0;
  }
  function m(O) {
    g(O, 0);
  }
  function g(O, k) {
    const q = s.newAttributes, B = s.enabledAttributes, G = s.attributeDivisors;
    q[O] = 1, B[O] === 0 && (r.enableVertexAttribArray(O), B[O] = 1), G[O] !== k && (r.vertexAttribDivisor(O, k), G[O] = k);
  }
  function E() {
    const O = s.newAttributes, k = s.enabledAttributes;
    for (let q = 0, B = k.length; q < B; q++) k[q] !== O[q] && (r.disableVertexAttribArray(q), k[q] = 0);
  }
  function R(O, k, q, B, G, Z, X) {
    X === !0 ? r.vertexAttribIPointer(O, k, q, G, Z) : r.vertexAttribPointer(O, k, q, B, G, Z);
  }
  function y(O, k, q, B) {
    v();
    const G = B.attributes, Z = q.getAttributes(), X = k.defaultAttributeValues;
    for (const te in Z) {
      const V = Z[te];
      if (V.location >= 0) {
        let K = G[te];
        if (K === void 0 && (te === "instanceMatrix" && O.instanceMatrix && (K = O.instanceMatrix), te === "instanceColor" && O.instanceColor && (K = O.instanceColor)), K !== void 0) {
          const ee = K.normalized, Ue = K.itemSize, ue = e.get(K);
          if (ue === void 0) continue;
          const Ye = ue.buffer, ke = ue.type, je = ue.bytesPerElement, Y = ke === r.INT || ke === r.UNSIGNED_INT || K.gpuType === Dl;
          if (K.isInterleavedBufferAttribute) {
            const $ = K.data, le = $.stride, Oe = K.offset;
            if ($.isInstancedInterleavedBuffer) {
              for (let xe = 0; xe < V.locationSize; xe++) g(V.location + xe, $.meshPerAttribute);
              O.isInstancedMesh !== !0 && B._maxInstanceCount === void 0 && (B._maxInstanceCount = $.meshPerAttribute * $.count);
            } else for (let xe = 0; xe < V.locationSize; xe++) m(V.location + xe);
            r.bindBuffer(r.ARRAY_BUFFER, Ye);
            for (let xe = 0; xe < V.locationSize; xe++) R(V.location + xe, Ue / V.locationSize, ke, ee, le * je, (Oe + Ue / V.locationSize * xe) * je, Y);
          } else {
            if (K.isInstancedBufferAttribute) {
              for (let $ = 0; $ < V.locationSize; $++) g(V.location + $, K.meshPerAttribute);
              O.isInstancedMesh !== !0 && B._maxInstanceCount === void 0 && (B._maxInstanceCount = K.meshPerAttribute * K.count);
            } else for (let $ = 0; $ < V.locationSize; $++) m(V.location + $);
            r.bindBuffer(r.ARRAY_BUFFER, Ye);
            for (let $ = 0; $ < V.locationSize; $++) R(V.location + $, Ue / V.locationSize, ke, ee, Ue * je, Ue / V.locationSize * $ * je, Y);
          }
        } else if (X !== void 0) {
          const ee = X[te];
          if (ee !== void 0) switch (ee.length) {
            case 2:
              r.vertexAttrib2fv(V.location, ee);
              break;
            case 3:
              r.vertexAttrib3fv(V.location, ee);
              break;
            case 4:
              r.vertexAttrib4fv(V.location, ee);
              break;
            default:
              r.vertexAttrib1fv(V.location, ee);
          }
        }
      }
    }
    E();
  }
  function S() {
    b();
    for (const O in i) {
      const k = i[O];
      for (const q in k) {
        const B = k[q];
        for (const G in B) {
          const Z = B[G];
          for (const X in Z) h(Z[X].object), delete Z[X];
          delete B[G];
        }
      }
      delete i[O];
    }
  }
  function T(O) {
    if (i[O.id] === void 0) return;
    const k = i[O.id];
    for (const q in k) {
      const B = k[q];
      for (const G in B) {
        const Z = B[G];
        for (const X in Z) h(Z[X].object), delete Z[X];
        delete B[G];
      }
    }
    delete i[O.id];
  }
  function A(O) {
    for (const k in i) {
      const q = i[k];
      for (const B in q) {
        const G = q[B];
        if (G[O.id] === void 0) continue;
        const Z = G[O.id];
        for (const X in Z) h(Z[X].object), delete Z[X];
        delete G[O.id];
      }
    }
  }
  function x(O) {
    for (const k in i) {
      const q = i[k], B = O.isInstancedMesh === !0 ? O.id : 0, G = q[B];
      if (G !== void 0) {
        for (const Z in G) {
          const X = G[Z];
          for (const te in X) h(X[te].object), delete X[te];
          delete G[Z];
        }
        delete q[B], Object.keys(q).length === 0 && delete i[k];
      }
    }
  }
  function b() {
    N(), a = !0, s !== n && (s = n, c(s.object));
  }
  function N() {
    n.geometry = null, n.program = null, n.wireframe = !1;
  }
  return { setup: o, reset: b, resetDefaultState: N, dispose: S, releaseStatesOfGeometry: T, releaseStatesOfObject: x, releaseStatesOfProgram: A, initAttributes: v, enableAttribute: m, disableUnusedAttributes: E };
}
function gv(r, e, t) {
  let i;
  function n(l) {
    i = l;
  }
  function s(l, c) {
    r.drawArrays(i, l, c), t.update(c, i, 1);
  }
  function a(l, c, h) {
    h !== 0 && (r.drawArraysInstanced(i, l, c, h), t.update(c, i, h));
  }
  function o(l, c, h) {
    if (h === 0) return;
    e.get("WEBGL_multi_draw").multiDrawArraysWEBGL(i, l, 0, c, 0, h);
    let d = 0;
    for (let u = 0; u < h; u++) d += c[u];
    t.update(d, i, 1);
  }
  this.setMode = n, this.render = s, this.renderInstances = a, this.renderMultiDraw = o;
}
function _v(r, e, t, i) {
  let n;
  function s() {
    if (n !== void 0) return n;
    if (e.has("EXT_texture_filter_anisotropic") === !0) {
      const A = e.get("EXT_texture_filter_anisotropic");
      n = r.getParameter(A.MAX_TEXTURE_MAX_ANISOTROPY_EXT);
    } else n = 0;
    return n;
  }
  function a(A) {
    return !(A !== li && i.convert(A) !== r.getParameter(r.IMPLEMENTATION_COLOR_READ_FORMAT));
  }
  function o(A) {
    const x = A === Ui && (e.has("EXT_color_buffer_half_float") || e.has("EXT_color_buffer_float"));
    return !(A !== $t && A !== oi && !x && i.convert(A) !== r.getParameter(r.IMPLEMENTATION_COLOR_READ_TYPE));
  }
  function l(A) {
    if (A === "highp") {
      if (r.getShaderPrecisionFormat(r.VERTEX_SHADER, r.HIGH_FLOAT).precision > 0 && r.getShaderPrecisionFormat(r.FRAGMENT_SHADER, r.HIGH_FLOAT).precision > 0) return "highp";
      A = "mediump";
    }
    return A === "mediump" && r.getShaderPrecisionFormat(r.VERTEX_SHADER, r.MEDIUM_FLOAT).precision > 0 && r.getShaderPrecisionFormat(r.FRAGMENT_SHADER, r.MEDIUM_FLOAT).precision > 0 ? "mediump" : "lowp";
  }
  let c = t.precision !== void 0 ? t.precision : "highp";
  const h = l(c);
  h !== c && (Re("WebGLRenderer:", c, "not supported, using", h, "instead."), c = h);
  const d = t.logarithmicDepthBuffer === !0, u = t.reversedDepthBuffer === !0 && e.has("EXT_clip_control");
  t.reversedDepthBuffer === !0 && u === !1 && Re("WebGLRenderer: Unable to use reversed depth buffer due to missing EXT_clip_control extension. Fallback to default depth buffer.");
  const p = r.getParameter(r.MAX_TEXTURE_IMAGE_UNITS), f = r.getParameter(r.MAX_VERTEX_TEXTURE_IMAGE_UNITS), v = r.getParameter(r.MAX_TEXTURE_SIZE), m = r.getParameter(r.MAX_CUBE_MAP_TEXTURE_SIZE), g = r.getParameter(r.MAX_VERTEX_ATTRIBS), E = r.getParameter(r.MAX_VERTEX_UNIFORM_VECTORS), R = r.getParameter(r.MAX_VARYING_VECTORS), y = r.getParameter(r.MAX_FRAGMENT_UNIFORM_VECTORS), S = r.getParameter(r.MAX_SAMPLES), T = r.getParameter(r.SAMPLES);
  return { isWebGL2: !0, getMaxAnisotropy: s, getMaxPrecision: l, textureFormatReadable: a, textureTypeReadable: o, precision: c, logarithmicDepthBuffer: d, reversedDepthBuffer: u, maxTextures: p, maxVertexTextures: f, maxTextureSize: v, maxCubemapSize: m, maxAttributes: g, maxVertexUniforms: E, maxVaryings: R, maxFragmentUniforms: y, maxSamples: S, samples: T };
}
function vv(r) {
  const e = this;
  let t = null, i = 0, n = !1, s = !1;
  const a = new ur(), o = new De(), l = { value: null, needsUpdate: !1 };
  this.uniform = l, this.numPlanes = 0, this.numIntersection = 0, this.init = function(d, u) {
    const p = d.length !== 0 || u || i !== 0 || n;
    return n = u, i = d.length, p;
  }, this.beginShadows = function() {
    s = !0, h(null);
  }, this.endShadows = function() {
    s = !1;
  }, this.setGlobalState = function(d, u) {
    t = h(d, u, 0);
  }, this.setState = function(d, u, p) {
    const f = d.clippingPlanes, v = d.clipIntersection, m = d.clipShadows, g = r.get(d);
    if (!n || f === null || f.length === 0 || s && !m) s ? h(null) : c();
    else {
      const E = s ? 0 : i, R = E * 4;
      let y = g.clippingState || null;
      l.value = y, y = h(f, u, R, p);
      for (let S = 0; S !== R; ++S) y[S] = t[S];
      g.clippingState = y, this.numIntersection = v ? this.numPlanes : 0, this.numPlanes += E;
    }
  };
  function c() {
    l.value !== t && (l.value = t, l.needsUpdate = i > 0), e.numPlanes = i, e.numIntersection = 0;
  }
  function h(d, u, p, f) {
    const v = d !== null ? d.length : 0;
    let m = null;
    if (v !== 0) {
      if (m = l.value, f !== !0 || m === null) {
        const g = p + v * 4, E = u.matrixWorldInverse;
        o.getNormalMatrix(E), (m === null || m.length < g) && (m = new Float32Array(g));
        for (let R = 0, y = p; R !== v; ++R, y += 4) a.copy(d[R]).applyMatrix4(E, o), a.normal.toArray(m, y), m[y + 3] = a.constant;
      }
      l.value = m, l.needsUpdate = !0;
    }
    return e.numPlanes = v, e.numIntersection = 0, m;
  }
}
const dn = 4, xv = 6, Mv = 20, yv = 256, Fn = new Ea(), eh = new be();
let ao = null, oo = 0, lo = 0, co = !1;
const Sv = new P(), br = new P();
class th {
  constructor(e) {
    this._renderer = e, this._pingPongRenderTarget = null, this._lodMax = 0, this._cubeSize = 0, this._sizeLods = [], this._lodMeshes = [], this._backgroundBox = null, this._cubemapMaterial = null, this._equirectMaterial = null, this._blurMaterial = null, this._ggxMaterial = null;
  }
  fromScene(e, t = 0, i = 0.1, n = 100, s = {}) {
    const { size: a = 256, position: o = Sv } = s;
    ao = this._renderer.getRenderTarget(), oo = this._renderer.getActiveCubeFace(), lo = this._renderer.getActiveMipmapLevel(), co = this._renderer.xr.enabled, this._renderer.xr.enabled = !1, this._setSize(a);
    const l = this._allocateTargets();
    return l.depthBuffer = !0, this._sceneToCubeUV(e, i, n, l, o), t > 0 && this._blur(l, 0, 0, t), this._applyPMREM(l), this._cleanup(l), l;
  }
  fromEquirectangular(e, t = null) {
    return this._fromTexture(e, t);
  }
  fromCubemap(e, t = null) {
    return this._fromTexture(e, t);
  }
  compileCubemapShader() {
    this._cubemapMaterial === null && (this._cubemapMaterial = nh(), this._compileMaterial(this._cubemapMaterial));
  }
  compileEquirectangularShader() {
    this._equirectMaterial === null && (this._equirectMaterial = rh(), this._compileMaterial(this._equirectMaterial));
  }
  dispose() {
    this._dispose(), this._cubemapMaterial !== null && this._cubemapMaterial.dispose(), this._equirectMaterial !== null && this._equirectMaterial.dispose(), this._backgroundBox !== null && (this._backgroundBox.geometry.dispose(), this._backgroundBox.material.dispose());
  }
  _setSize(e) {
    this._lodMax = Math.floor(Math.log2(e)), this._cubeSize = Math.pow(2, this._lodMax);
  }
  _dispose() {
    this._blurMaterial !== null && this._blurMaterial.dispose(), this._ggxMaterial !== null && this._ggxMaterial.dispose(), this._pingPongRenderTarget !== null && this._pingPongRenderTarget.dispose();
    for (let e = 0; e < this._lodMeshes.length; e++) this._lodMeshes[e].geometry.dispose();
  }
  _cleanup(e) {
    this._renderer.setRenderTarget(ao, oo, lo), this._renderer.xr.enabled = co, e.scissorTest = !1, Qr(e, 0, 0, e.width, e.height);
  }
  _fromTexture(e, t) {
    e.mapping === Nr || e.mapping === gn ? this._setSize(e.image.length === 0 ? 16 : e.image[0].width || e.image[0].image.width) : this._setSize(e.image.width / 4), ao = this._renderer.getRenderTarget(), oo = this._renderer.getActiveCubeFace(), lo = this._renderer.getActiveMipmapLevel(), co = this._renderer.xr.enabled, this._renderer.xr.enabled = !1;
    const i = t || this._allocateTargets();
    return this._textureToCubeUV(e, i), this._applyPMREM(i), this._cleanup(i), i;
  }
  _allocateTargets() {
    const e = 3 * Math.max(this._cubeSize, 112), t = 4 * this._cubeSize, i = { magFilter: Pt, minFilter: Pt, generateMipmaps: !1, type: Ui, format: li, colorSpace: ei, depthBuffer: !1 }, n = ih(e, t, i);
    if (this._pingPongRenderTarget === null || this._pingPongRenderTarget.width !== e || this._pingPongRenderTarget.height !== t) {
      this._pingPongRenderTarget !== null && this._dispose(), this._pingPongRenderTarget = ih(e, t, i);
      const { _lodMax: s } = this;
      ({ lodMeshes: this._lodMeshes, sizeLods: this._sizeLods } = Tv(s)), this._blurMaterial = bv(s, e, t), this._ggxMaterial = Ev(s, e, t);
    }
    return n;
  }
  _compileMaterial(e) {
    const t = new Wt(new yt(), e);
    this._renderer.compile(t, Fn);
  }
  _sceneToCubeUV(e, t, i, n, s) {
    const a = new zt(90, 1, t, i), o = [1, -1, 1, 1, 1, 1], l = [1, 1, 1, -1, -1, -1], c = this._renderer, h = c.autoClear, d = c.toneMapping;
    c.getClearColor(eh), c.toneMapping = Ii, c.autoClear = !1, c.state.buffers.depth.getReversed() && (c.setRenderTarget(n), c.clearDepth(), c.setRenderTarget(null)), this._backgroundBox === null && (this._backgroundBox = new Wt(new ps(), new Ki({ name: "PMREM.Background", side: Gt, depthWrite: !1, depthTest: !1 })));
    const u = this._backgroundBox, p = u.material;
    let f = !1;
    const v = e.background;
    v ? v.isColor && (p.color.copy(v), e.background = null, f = !0) : (p.color.copy(eh), f = !0);
    for (let m = 0; m < 6; m++) {
      const g = m % 3;
      g === 0 ? (a.up.set(0, o[m], 0), a.position.set(s.x, s.y, s.z), a.lookAt(s.x + l[m], s.y, s.z)) : g === 1 ? (a.up.set(0, 0, o[m]), a.position.set(s.x, s.y, s.z), a.lookAt(s.x, s.y + l[m], s.z)) : (a.up.set(0, o[m], 0), a.position.set(s.x, s.y, s.z), a.lookAt(s.x, s.y, s.z + l[m]));
      const E = this._cubeSize;
      Qr(n, g * E, m > 2 ? E : 0, E, E), c.setRenderTarget(n), f && c.render(u, a), c.render(e, a);
    }
    c.toneMapping = d, c.autoClear = h, e.background = v;
  }
  _textureToCubeUV(e, t) {
    const i = this._renderer, n = e.mapping === Nr || e.mapping === gn;
    n ? (this._cubemapMaterial === null && (this._cubemapMaterial = nh()), this._cubemapMaterial.uniforms.flipEnvMap.value = e.isRenderTargetTexture === !1 ? -1 : 1) : this._equirectMaterial === null && (this._equirectMaterial = rh());
    const s = n ? this._cubemapMaterial : this._equirectMaterial, a = this._lodMeshes[0];
    a.material = s;
    const o = s.uniforms;
    o.envMap.value = e;
    const l = this._cubeSize;
    Qr(t, 0, 0, 3 * l, 2 * l), i.setRenderTarget(t), i.render(a, Fn);
  }
  _applyPMREM(e) {
    const t = this._renderer, i = t.autoClear;
    t.autoClear = !1;
    const n = this._lodMeshes.length;
    for (let s = 1; s < n; s++) this._applyGGXFilter(e, s - 1, s);
    t.autoClear = i;
  }
  _applyGGXFilter(e, t, i) {
    const n = this._renderer, s = this._pingPongRenderTarget, a = this._ggxMaterial, o = this._lodMeshes[i];
    o.material = a;
    const l = a.uniforms, c = i / (this._lodMeshes.length - 1), h = t / (this._lodMeshes.length - 1), d = Math.sqrt(c * c - h * h), u = c * 1.25, p = d * u, { _lodMax: f } = this, v = this._sizeLods[i], m = 3 * v * (i > f - dn ? i - f + dn : 0), g = 4 * (this._cubeSize - v);
    l.envMap.value = e.texture, l.roughness.value = p, l.mipInt.value = f - t, Qr(s, m, g, 3 * v, 2 * v), n.setRenderTarget(s), n.render(o, Fn), l.envMap.value = s.texture, l.roughness.value = 0, l.mipInt.value = f - i, Qr(e, m, g, 3 * v, 2 * v), n.setRenderTarget(e), n.render(o, Fn);
  }
  _blur(e, t, i, n) {
    const s = this._pingPongRenderTarget, a = Math.min(n, Math.PI) / Math.SQRT2;
    this._blurPass(e, s, t, i, a), this._blurPass(s, e, i, i, a);
  }
  _blurPass(e, t, i, n, s) {
    const a = this._renderer, o = this._blurMaterial, l = this._lodMeshes[n];
    l.material = o;
    const c = o.uniforms;
    c.envMap.value = e.texture, c.sigma.value = s, c.mipInt.value = this._lodMax - i;
    const h = this._sizeLods[n], d = 3 * h * (n > this._lodMax - dn ? n - this._lodMax + dn : 0), u = 4 * (this._cubeSize - h);
    Qr(t, d, u, 3 * h, 2 * h), a.setRenderTarget(t), a.render(l, Fn);
  }
}
function Tv(r) {
  const e = [], t = [];
  let i = r;
  const n = r - dn + 1 + xv;
  for (let s = 0; s < n; s++) {
    const a = Math.pow(2, i);
    e.push(a);
    const o = 1 / (a - 2), l = -o, c = 1 + o, h = [l, l, c, l, c, c, l, l, c, c, l, c], d = 6, u = 6, p = 3, f = new Float32Array(p * u * d), v = new Float32Array(p * u * d);
    for (let g = 0; g < d; g++) {
      const E = g % 3 * 2 / 3 - 1, R = g > 2 ? 0 : -1, y = [E, R, 0, E + 2 / 3, R, 0, E + 2 / 3, R + 1, 0, E, R, 0, E + 2 / 3, R + 1, 0, E, R + 1, 0];
      f.set(y, p * u * g);
      for (let S = 0; S < u; S++) {
        const T = h[S * 2] * 2 - 1, A = h[S * 2 + 1] * 2 - 1;
        g === 0 ? br.set(1, A, T) : g === 1 ? br.set(-T, 1, -A) : g === 2 ? br.set(-T, A, 1) : g === 3 ? br.set(-1, A, -T) : g === 4 ? br.set(-T, -1, A) : br.set(T, A, -1), br.toArray(v, (g * u + S) * p);
      }
    }
    const m = new yt();
    m.setAttribute("position", new nt(f, p)), m.setAttribute("outputDirection", new nt(v, p)), t.push(new Wt(m, null)), i > dn && i--;
  }
  return { lodMeshes: t, sizeLods: e };
}
function ih(r, e, t) {
  const i = new _i(r, e, t);
  return i.texture.mapping = xa, i.texture.name = "PMREM.cubeUv", i.scissorTest = !0, i;
}
function Qr(r, e, t, i, n) {
  r.viewport.set(e, t, i, n), r.scissor.set(e, t, i, n);
}
function Ev(r, e, t) {
  return new xi({ name: "PMREMGGXConvolution", defines: { GGX_SAMPLES: yv, CUBEUV_TEXEL_WIDTH: 1 / e, CUBEUV_TEXEL_HEIGHT: 1 / t, CUBEUV_MAX_MIP: `${r}.0` }, uniforms: { envMap: { value: null }, roughness: { value: 0 }, mipInt: { value: 0 } }, vertexShader: ba(), fragmentShader: `

			precision highp float;
			precision highp int;

			varying vec3 vOutputDirection;

			uniform sampler2D envMap;
			uniform float roughness;
			uniform float mipInt;

			#define ENVMAP_TYPE_CUBE_UV
			#include <cube_uv_reflection_fragment>

			#define PI 3.14159265359

			// Van der Corput radical inverse
			float radicalInverse_VdC(uint bits) {
				bits = (bits << 16u) | (bits >> 16u);
				bits = ((bits & 0x55555555u) << 1u) | ((bits & 0xAAAAAAAAu) >> 1u);
				bits = ((bits & 0x33333333u) << 2u) | ((bits & 0xCCCCCCCCu) >> 2u);
				bits = ((bits & 0x0F0F0F0Fu) << 4u) | ((bits & 0xF0F0F0F0u) >> 4u);
				bits = ((bits & 0x00FF00FFu) << 8u) | ((bits & 0xFF00FF00u) >> 8u);
				return float(bits) * 2.3283064365386963e-10; // / 0x100000000
			}

			// Hammersley sequence
			vec2 hammersley(uint i, uint N) {
				return vec2(float(i) / float(N), radicalInverse_VdC(i));
			}

			// GGX VNDF importance sampling (Eric Heitz 2018)
			// "Sampling the GGX Distribution of Visible Normals"
			// https://jcgt.org/published/0007/04/01/
			vec3 importanceSampleGGX_VNDF(vec2 Xi, vec3 V, float roughness) {
				float alpha = roughness * roughness;

				// Section 4.1: Orthonormal basis
				vec3 T1 = vec3(1.0, 0.0, 0.0);
				vec3 T2 = cross(V, T1);

				// Section 4.2: Parameterization of projected area
				float r = sqrt(Xi.x);
				float phi = 2.0 * PI * Xi.y;
				float t1 = r * cos(phi);
				float t2 = r * sin(phi);
				float s = 0.5 * (1.0 + V.z);
				t2 = (1.0 - s) * sqrt(1.0 - t1 * t1) + s * t2;

				// Section 4.3: Reprojection onto hemisphere
				vec3 Nh = t1 * T1 + t2 * T2 + sqrt(max(0.0, 1.0 - t1 * t1 - t2 * t2)) * V;

				// Section 3.4: Transform back to ellipsoid configuration
				return normalize(vec3(alpha * Nh.x, alpha * Nh.y, max(0.0, Nh.z)));
			}

			void main() {
				vec3 N = normalize(vOutputDirection);
				vec3 V = N; // Assume view direction equals normal for pre-filtering

				vec3 prefilteredColor = vec3(0.0);
				float totalWeight = 0.0;

				// For very low roughness, just sample the environment directly
				if (roughness < 0.001) {
					gl_FragColor = vec4(bilinearCubeUV(envMap, N, mipInt), 1.0);
					return;
				}

				// Tangent space basis for VNDF sampling
				vec3 up = abs(N.z) < 0.999 ? vec3(0.0, 0.0, 1.0) : vec3(1.0, 0.0, 0.0);
				vec3 tangent = normalize(cross(up, N));
				vec3 bitangent = cross(N, tangent);

				for(uint i = 0u; i < uint(GGX_SAMPLES); i++) {
					vec2 Xi = hammersley(i, uint(GGX_SAMPLES));

					// For PMREM, V = N, so in tangent space V is always (0, 0, 1)
					vec3 H_tangent = importanceSampleGGX_VNDF(Xi, vec3(0.0, 0.0, 1.0), roughness);

					// Transform H back to world space
					vec3 H = normalize(tangent * H_tangent.x + bitangent * H_tangent.y + N * H_tangent.z);
					vec3 L = normalize(2.0 * dot(V, H) * H - V);

					float NdotL = max(dot(N, L), 0.0);

					if(NdotL > 0.0) {
						// Sample environment at fixed mip level
						// VNDF importance sampling handles the distribution filtering
						vec3 sampleColor = bilinearCubeUV(envMap, L, mipInt);

						// Weight by NdotL for the split-sum approximation
						// VNDF PDF naturally accounts for the visible microfacet distribution
						prefilteredColor += sampleColor * NdotL;
						totalWeight += NdotL;
					}
				}

				if (totalWeight > 0.0) {
					prefilteredColor = prefilteredColor / totalWeight;
				}

				gl_FragColor = vec4(prefilteredColor, 1.0);
			}
		`, blending: $i, depthTest: !1, depthWrite: !1 });
}
function bv(r, e, t) {
  return new xi({ name: "SphericalGaussianBlur", defines: { SAMPLES: Mv, CUBEUV_TEXEL_WIDTH: 1 / e, CUBEUV_TEXEL_HEIGHT: 1 / t, CUBEUV_MAX_MIP: `${r}.0` }, uniforms: { envMap: { value: null }, sigma: { value: 0 }, mipInt: { value: 0 } }, vertexShader: ba(), fragmentShader: `

			precision highp float;
			precision highp int;

			varying vec3 vOutputDirection;

			uniform sampler2D envMap;
			uniform float sigma;
			uniform float mipInt;

			#define ENVMAP_TYPE_CUBE_UV
			#include <cube_uv_reflection_fragment>

			#define PI 3.14159265359
			#define GOLDEN_ANGLE 2.39996322973

			void main() {

				if ( sigma == 0.0 ) {

					gl_FragColor = vec4( bilinearCubeUV( envMap, vOutputDirection, mipInt ), 1.0 );
					return;

				}

				vec3 outputDirection = normalize( vOutputDirection );

				vec3 up = abs( outputDirection.z ) < 0.999 ? vec3( 0.0, 0.0, 1.0 ) : vec3( 1.0, 0.0, 0.0 );
				vec3 tangent = normalize( cross( up, outputDirection ) );
				vec3 bitangent = cross( outputDirection, tangent );

				// Truncate the kernel at three standard deviations or at the antipode.
				float thetaMax = min( 3.0 * sigma, PI );
				float truncation = 1.0 - exp( - 0.5 * thetaMax * thetaMax / ( sigma * sigma ) );

				vec3 accumColor = vec3( 0.0 );
				float accumWeight = 0.0;

				for ( int i = 0; i < SAMPLES; i ++ ) {

					// Stratified inverse-CDF sampling of the Gaussian, placed on a golden-angle spiral.
					float stratum = ( float( i ) + 0.5 ) / float( SAMPLES );
					float theta = sigma * sqrt( - 2.0 * log( 1.0 - stratum * truncation ) );
					float phi = float( i ) * GOLDEN_ANGLE;

					vec3 offset = cos( phi ) * tangent + sin( phi ) * bitangent;
					vec3 sampleDirection = cos( theta ) * outputDirection + sin( theta ) * offset;

					// Correct the planar sample density to solid angle.
					float weight = sin( theta ) / theta;

					accumColor += weight * bilinearCubeUV( envMap, sampleDirection, mipInt );
					accumWeight += weight;

				}

				gl_FragColor = vec4( accumColor / accumWeight, 1.0 );

			}
		`, blending: $i, depthTest: !1, depthWrite: !1 });
}
function rh() {
  return new xi({ name: "EquirectangularToCubeUV", uniforms: { envMap: { value: null } }, vertexShader: ba(), fragmentShader: `

			precision mediump float;
			precision mediump int;

			varying vec3 vOutputDirection;

			uniform sampler2D envMap;

			#include <common>

			void main() {

				vec3 outputDirection = normalize( vOutputDirection );
				vec2 uv = equirectUv( outputDirection );

				gl_FragColor = vec4( texture2D ( envMap, uv ).rgb, 1.0 );

			}
		`, blending: $i, depthTest: !1, depthWrite: !1 });
}
function nh() {
  return new xi({ name: "CubemapToCubeUV", uniforms: { envMap: { value: null }, flipEnvMap: { value: -1 } }, vertexShader: ba(), fragmentShader: `

			precision mediump float;
			precision mediump int;

			uniform float flipEnvMap;

			varying vec3 vOutputDirection;

			uniform samplerCube envMap;

			void main() {

				gl_FragColor = textureCube( envMap, vec3( flipEnvMap * vOutputDirection.x, vOutputDirection.yz ) );

			}
		`, blending: $i, depthTest: !1, depthWrite: !1 });
}
function ba() {
  return `

		precision mediump float;
		precision mediump int;

		attribute vec3 outputDirection;

		varying vec3 vOutputDirection;

		void main() {

			vOutputDirection = outputDirection;
			gl_Position = vec4( position, 1.0 );

		}
	`;
}
class md extends _i {
  constructor(e = 1, t = {}) {
    super(e, e, t), this.isWebGLCubeRenderTarget = !0;
    const i = { width: e, height: e, depth: 1 }, n = [i, i, i, i, i, i];
    this.texture = new id(n), this._setTextureOptions(t), this.texture.isRenderTargetTexture = !0;
  }
  fromEquirectangularTexture(e, t) {
    this.texture.type = t.type, this.texture.colorSpace = t.colorSpace, this.texture.generateMipmaps = t.generateMipmaps, this.texture.minFilter = t.minFilter, this.texture.magFilter = t.magFilter;
    const i = { uniforms: { tEquirect: { value: null } }, vertexShader: `

				varying vec3 vWorldDirection;

				vec3 transformDirection( in vec3 dir, in mat4 matrix ) {

					return normalize( ( matrix * vec4( dir, 0.0 ) ).xyz );

				}

				void main() {

					vWorldDirection = transformDirection( position, modelMatrix );

					#include <begin_vertex>
					#include <project_vertex>

				}
			`, fragmentShader: `

				uniform sampler2D tEquirect;

				varying vec3 vWorldDirection;

				#include <common>

				void main() {

					vec3 direction = normalize( vWorldDirection );

					vec2 sampleUV = equirectUv( direction );

					gl_FragColor = texture2D( tEquirect, sampleUV );

				}
			` }, n = new ps(5, 5, 5), s = new xi({ name: "CubemapFromEquirect", uniforms: xn(i.uniforms), vertexShader: i.vertexShader, fragmentShader: i.fragmentShader, side: Gt, blending: $i });
    s.uniforms.tEquirect.value = t;
    const a = new Wt(n, s), o = t.minFilter;
    return t.minFilter === Yi && (t.minFilter = Pt), new fm(1, 10, this).update(e, a), t.minFilter = o, a.geometry.dispose(), a.material.dispose(), this;
  }
  clear(e, t = !0, i = !0, n = !0) {
    const s = e.getRenderTarget();
    for (let a = 0; a < 6; a++) e.setRenderTarget(this, a), e.clear(t, i, n);
    e.setRenderTarget(s);
  }
}
function wv(r) {
  let e = /* @__PURE__ */ new WeakMap(), t = /* @__PURE__ */ new WeakMap(), i = null;
  function n(u, p = !1) {
    return u == null ? null : p ? a(u) : s(u);
  }
  function s(u) {
    if (u && u.isTexture) {
      const p = u.mapping;
      if (p === La || p === Ia) if (e.has(u)) {
        const f = e.get(u).texture;
        return o(f, u.mapping);
      } else {
        const f = u.image;
        if (f && f.height > 0) {
          const v = new md(f.height);
          return v.fromEquirectangularTexture(r, u), e.set(u, v), u.addEventListener("dispose", c), o(v.texture, u.mapping);
        } else return null;
      }
    }
    return u;
  }
  function a(u) {
    if (u && u.isTexture) {
      const p = u.mapping, f = p === La || p === Ia, v = p === Nr || p === gn;
      if (f || v) {
        let m = t.get(u);
        const g = m !== void 0 ? m.texture.pmremVersion : 0;
        if (u.isRenderTargetTexture && u.pmremVersion !== g) return i === null && (i = new th(r)), m = f ? i.fromEquirectangular(u, m) : i.fromCubemap(u, m), m.texture.pmremVersion = u.pmremVersion, t.set(u, m), m.texture;
        if (m !== void 0) return m.texture;
        {
          const E = u.image;
          return f && E && E.height > 0 || v && E && l(E) ? (i === null && (i = new th(r)), m = f ? i.fromEquirectangular(u) : i.fromCubemap(u), m.texture.pmremVersion = u.pmremVersion, t.set(u, m), u.addEventListener("dispose", h), m.texture) : null;
        }
      }
    }
    return u;
  }
  function o(u, p) {
    return p === La ? u.mapping = Nr : p === Ia && (u.mapping = gn), u;
  }
  function l(u) {
    let p = 0;
    const f = 6;
    for (let v = 0; v < f; v++) u[v] !== void 0 && p++;
    return p === f;
  }
  function c(u) {
    const p = u.target;
    p.removeEventListener("dispose", c);
    const f = e.get(p);
    f !== void 0 && (e.delete(p), f.dispose());
  }
  function h(u) {
    const p = u.target;
    p.removeEventListener("dispose", h);
    const f = t.get(p);
    f !== void 0 && (t.delete(p), f.dispose());
  }
  function d() {
    e = /* @__PURE__ */ new WeakMap(), t = /* @__PURE__ */ new WeakMap(), i !== null && (i.dispose(), i = null);
  }
  return { get: n, dispose: d };
}
function Av(r) {
  const e = {};
  function t(i) {
    if (e[i] !== void 0) return e[i];
    const n = r.getExtension(i);
    return e[i] = n, n;
  }
  return { has: function(i) {
    return t(i) !== null;
  }, init: function() {
    t("EXT_color_buffer_float"), t("WEBGL_clip_cull_distance"), t("OES_texture_float_linear"), t("EXT_color_buffer_half_float"), t("WEBGL_multisampled_render_to_texture"), t("WEBGL_render_shared_exponent");
  }, get: function(i) {
    const n = t(i);
    return n === null && pn("WebGLRenderer: " + i + " extension not supported."), n;
  } };
}
function Rv(r, e, t, i) {
  const n = {}, s = /* @__PURE__ */ new WeakMap();
  function a(d) {
    const u = d.target;
    u.index !== null && e.remove(u.index);
    for (const f in u.attributes) e.remove(u.attributes[f]);
    u.removeEventListener("dispose", a), delete n[u.id];
    const p = s.get(u);
    p && (e.remove(p), s.delete(u)), i.releaseStatesOfGeometry(u), u.isInstancedBufferGeometry === !0 && delete u._maxInstanceCount, t.memory.geometries--;
  }
  function o(d, u) {
    return n[u.id] === !0 || (u.addEventListener("dispose", a), n[u.id] = !0, t.memory.geometries++), u;
  }
  function l(d) {
    const u = d.attributes;
    for (const p in u) e.update(u[p], r.ARRAY_BUFFER);
  }
  function c(d) {
    const u = [], p = d.index, f = d.attributes.position;
    let v = 0;
    if (f === void 0) return;
    if (p !== null) {
      const E = p.array;
      v = p.version;
      for (let R = 0, y = E.length; R < y; R += 3) {
        const S = E[R + 0], T = E[R + 1], A = E[R + 2];
        u.push(S, T, T, A, A, S);
      }
    } else {
      const E = f.array;
      v = f.version;
      for (let R = 0, y = E.length / 3 - 1; R < y; R += 3) {
        const S = R + 0, T = R + 1, A = R + 2;
        u.push(S, T, T, A, A, S);
      }
    }
    const m = new (f.count >= 65535 ? Qu : $u)(u, 1);
    m.version = v;
    const g = s.get(d);
    g && e.remove(g), s.set(d, m);
  }
  function h(d) {
    const u = s.get(d);
    if (u) {
      const p = d.index;
      p !== null && u.version < p.version && c(d);
    } else c(d);
    return s.get(d);
  }
  return { get: o, update: l, getWireframeAttribute: h };
}
function Pv(r, e, t) {
  let i;
  function n(d) {
    i = d;
  }
  let s, a;
  function o(d) {
    s = d.type, a = d.bytesPerElement;
  }
  function l(d, u) {
    r.drawElements(i, u, s, d * a), t.update(u, i, 1);
  }
  function c(d, u, p) {
    p !== 0 && (r.drawElementsInstanced(i, u, s, d * a, p), t.update(u, i, p));
  }
  function h(d, u, p) {
    if (p === 0) return;
    e.get("WEBGL_multi_draw").multiDrawElementsWEBGL(i, u, 0, s, d, 0, p);
    let f = 0;
    for (let v = 0; v < p; v++) f += u[v];
    t.update(f, i, 1);
  }
  this.setMode = n, this.setIndex = o, this.render = l, this.renderInstances = c, this.renderMultiDraw = h;
}
function Cv(r) {
  const e = { geometries: 0, textures: 0 }, t = { frame: 0, calls: 0, triangles: 0, points: 0, lines: 0 };
  function i(s, a, o) {
    switch (t.calls++, a) {
      case r.TRIANGLES:
        t.triangles += o * (s / 3);
        break;
      case r.LINES:
        t.lines += o * (s / 2);
        break;
      case r.LINE_STRIP:
        t.lines += o * (s - 1);
        break;
      case r.LINE_LOOP:
        t.lines += o * s;
        break;
      case r.POINTS:
        t.points += o * s;
        break;
      default:
        Be("WebGLInfo: Unknown draw mode:", a);
        break;
    }
  }
  function n() {
    t.calls = 0, t.triangles = 0, t.points = 0, t.lines = 0;
  }
  return { memory: e, render: t, programs: null, autoReset: !0, reset: n, update: i };
}
function Lv(r, e, t) {
  const i = /* @__PURE__ */ new WeakMap(), n = new gt();
  function s(a, o, l) {
    const c = a.morphTargetInfluences, h = o.morphAttributes.position || o.morphAttributes.normal || o.morphAttributes.color, d = h !== void 0 ? h.length : 0;
    let u = i.get(o);
    if (u === void 0 || u.count !== d) {
      let f = function() {
        b.dispose(), i.delete(o), o.removeEventListener("dispose", f);
      };
      var p = f;
      u !== void 0 && u.texture.dispose();
      const v = o.morphAttributes.position !== void 0, m = o.morphAttributes.normal !== void 0, g = o.morphAttributes.color !== void 0, E = o.morphAttributes.position || [], R = o.morphAttributes.normal || [], y = o.morphAttributes.color || [];
      let S = 0;
      v === !0 && (S = 1), m === !0 && (S = 2), g === !0 && (S = 3);
      let T = o.attributes.position.count * S, A = 1;
      T > e.maxTextureSize && (A = Math.ceil(T / e.maxTextureSize), T = e.maxTextureSize);
      const x = new Float32Array(T * A * 4 * d), b = new ju(x, T, A, d);
      b.type = oi, b.needsUpdate = !0;
      const N = S * 4;
      for (let O = 0; O < d; O++) {
        const k = E[O], q = R[O], B = y[O], G = T * A * 4 * O;
        for (let Z = 0; Z < k.count; Z++) {
          const X = Z * N;
          v === !0 && (n.fromBufferAttribute(k, Z), x[G + X + 0] = n.x, x[G + X + 1] = n.y, x[G + X + 2] = n.z, x[G + X + 3] = 0), m === !0 && (n.fromBufferAttribute(q, Z), x[G + X + 4] = n.x, x[G + X + 5] = n.y, x[G + X + 6] = n.z, x[G + X + 7] = 0), g === !0 && (n.fromBufferAttribute(B, Z), x[G + X + 8] = n.x, x[G + X + 9] = n.y, x[G + X + 10] = n.z, x[G + X + 11] = B.itemSize === 4 ? n.w : 1);
        }
      }
      u = { count: d, texture: b, size: new We(T, A) }, i.set(o, u), o.addEventListener("dispose", f);
    }
    if (a.isInstancedMesh === !0 && a.morphTexture !== null) l.getUniforms().setValue(r, "morphTexture", a.morphTexture, t);
    else {
      let f = 0;
      for (let m = 0; m < c.length; m++) f += c[m];
      const v = o.morphTargetsRelative ? 1 : 1 - f;
      l.getUniforms().setValue(r, "morphTargetBaseInfluence", v), l.getUniforms().setValue(r, "morphTargetInfluences", c);
    }
    l.getUniforms().setValue(r, "morphTargetsTexture", u.texture, t), l.getUniforms().setValue(r, "morphTargetsTextureSize", u.size);
  }
  return { update: s };
}
function Iv(r, e, t, i, n) {
  let s = /* @__PURE__ */ new WeakMap();
  function a(c) {
    const h = n.render.frame, d = c.geometry, u = e.get(c, d);
    if (s.get(u) !== h && (e.update(u), s.set(u, h)), c.isInstancedMesh && (c.hasEventListener("dispose", l) === !1 && c.addEventListener("dispose", l), s.get(c) !== h && (t.update(c.instanceMatrix, r.ARRAY_BUFFER), c.instanceColor !== null && t.update(c.instanceColor, r.ARRAY_BUFFER), s.set(c, h))), c.isSkinnedMesh) {
      const p = c.skeleton;
      s.get(p) !== h && (p.update(), s.set(p, h));
    }
    return u;
  }
  function o() {
    s = /* @__PURE__ */ new WeakMap();
  }
  function l(c) {
    const h = c.target;
    h.removeEventListener("dispose", l), i.releaseStatesOfObject(h), t.remove(h.instanceMatrix), h.instanceColor !== null && t.remove(h.instanceColor);
  }
  return { update: a, dispose: o };
}
const Nv = { [Eu]: "LINEAR_TONE_MAPPING", [bu]: "REINHARD_TONE_MAPPING", [wu]: "CINEON_TONE_MAPPING", [Au]: "ACES_FILMIC_TONE_MAPPING", [Pu]: "AGX_TONE_MAPPING", [Cu]: "NEUTRAL_TONE_MAPPING", [Ru]: "CUSTOM_TONE_MAPPING" };
function Uv(r, e, t, i, n, s) {
  const a = new _i(e, t, { type: r, depthBuffer: n, stencilBuffer: s, samples: i ? 4 : 0, storeMultisampledDepthBuffer: !1, storeMultisampledStencilBuffer: !1, resolveDepthBuffer: !1, resolveStencilBuffer: !1 });
  let o = null, l = null;
  const c = new yt();
  c.setAttribute("position", new Jt([-1, 3, 0, -1, -1, 0, 3, -1, 0], 3)), c.setAttribute("uv", new Jt([0, 2, 0, 0, 2, 0], 2));
  const h = new Xf({ uniforms: { tDiffuse: { value: null } }, vertexShader: `
			precision highp float;

			uniform mat4 modelViewMatrix;
			uniform mat4 projectionMatrix;

			attribute vec3 position;
			attribute vec2 uv;

			varying vec2 vUv;

			void main() {
				vUv = uv;
				gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );
			}`, fragmentShader: `
			precision highp float;

			uniform sampler2D tDiffuse;

			varying vec2 vUv;

			#include <tonemapping_pars_fragment>
			#include <colorspace_pars_fragment>

			void main() {
				gl_FragColor = texture2D( tDiffuse, vUv );

				#ifdef LINEAR_TONE_MAPPING
					gl_FragColor.rgb = LinearToneMapping( gl_FragColor.rgb );
				#elif defined( REINHARD_TONE_MAPPING )
					gl_FragColor.rgb = ReinhardToneMapping( gl_FragColor.rgb );
				#elif defined( CINEON_TONE_MAPPING )
					gl_FragColor.rgb = CineonToneMapping( gl_FragColor.rgb );
				#elif defined( ACES_FILMIC_TONE_MAPPING )
					gl_FragColor.rgb = ACESFilmicToneMapping( gl_FragColor.rgb );
				#elif defined( AGX_TONE_MAPPING )
					gl_FragColor.rgb = AgXToneMapping( gl_FragColor.rgb );
				#elif defined( NEUTRAL_TONE_MAPPING )
					gl_FragColor.rgb = NeutralToneMapping( gl_FragColor.rgb );
				#elif defined( CUSTOM_TONE_MAPPING )
					gl_FragColor.rgb = CustomToneMapping( gl_FragColor.rgb );
				#endif

				#ifdef SRGB_TRANSFER
					gl_FragColor = sRGBTransferOETF( gl_FragColor );
				#endif
			}`, depthTest: !1, depthWrite: !1 }), d = new Wt(c, h), u = new Ea(-1, 1, 1, -1, 0, 1);
  let p = null, f = null, v = !1, m, g = null, E = [], R = !1;
  this.setSize = function(y, S) {
    a.setSize(y, S), o !== null && o.setSize(y, S), l !== null && l.setSize(y, S);
    for (let T = 0; T < E.length; T++) {
      const A = E[T];
      A.setSize && A.setSize(y, S);
    }
  }, this.setEffects = function(y) {
    E = y, R = E.length > 0 && E[0].isRenderPass === !0;
    const S = a.width, T = a.height;
    E.length > 0 && o === null && (o = new _i(S, T, { type: Ui, depthBuffer: !1, stencilBuffer: !1 }), l = new _i(S, T, { type: Ui, depthBuffer: !1, stencilBuffer: !1 }));
    for (let A = 0; A < E.length; A++) {
      const x = E[A];
      x.setSize && x.setSize(S, T);
    }
  }, this.begin = function(y, S) {
    if (v || y.toneMapping === Ii && E.length === 0) return !1;
    if (g = S, S !== null) {
      const T = S.width, A = S.height;
      (a.width !== T || a.height !== A) && this.setSize(T, A);
    }
    return R === !1 && y.setRenderTarget(a), m = y.toneMapping, y.toneMapping = Ii, !0;
  }, this.hasRenderPass = function() {
    return R;
  }, this.end = function(y, S) {
    y.toneMapping = m, v = !0;
    let T = a, A = o;
    for (let x = 0; x < E.length; x++) {
      const b = E[x];
      b.enabled !== !1 && (b.render(y, A, T, S), b.needsSwap !== !1 && (T = A, A = A === o ? l : o));
    }
    if (p !== y.outputColorSpace || f !== y.toneMapping) {
      p = y.outputColorSpace, f = y.toneMapping, h.defines = {}, Ze.getTransfer(p) === ot && (h.defines.SRGB_TRANSFER = "");
      const x = Nv[f];
      x && (h.defines[x] = ""), h.needsUpdate = !0;
    }
    h.uniforms.tDiffuse.value = T.texture, y.setRenderTarget(g), y.render(d, u), g = null, v = !1;
  }, this.isCompositing = function() {
    return v;
  }, this.dispose = function() {
    a.dispose(), o !== null && o.dispose(), l !== null && l.dispose(), c.dispose(), h.dispose();
  };
}
const gd = new Nt(), Sl = new cs(1, 1), _d = new ju(), vd = new vf(), xd = new id(), sh = [], ah = [], oh = new Float32Array(16), lh = new Float32Array(9), ch = new Float32Array(4);
function bn(r, e, t) {
  const i = r[0];
  if (i <= 0 || i > 0) return r;
  const n = e * t;
  let s = sh[n];
  if (s === void 0 && (s = new Float32Array(n), sh[n] = s), e !== 0) {
    i.toArray(s, 0);
    for (let a = 1, o = 0; a !== e; ++a) o += t, r[a].toArray(s, o);
  }
  return s;
}
function Ct(r, e) {
  if (r.length !== e.length) return !1;
  for (let t = 0, i = r.length; t < i; t++) if (r[t] !== e[t]) return !1;
  return !0;
}
function Lt(r, e) {
  for (let t = 0, i = e.length; t < i; t++) r[t] = e[t];
}
function wa(r, e) {
  let t = ah[e];
  t === void 0 && (t = new Int32Array(e), ah[e] = t);
  for (let i = 0; i !== e; ++i) t[i] = r.allocateTextureUnit();
  return t;
}
function Dv(r, e) {
  const t = this.cache;
  t[0] !== e && (r.uniform1f(this.addr, e), t[0] = e);
}
function Ov(r, e) {
  const t = this.cache;
  if (e.x !== void 0) (t[0] !== e.x || t[1] !== e.y) && (r.uniform2f(this.addr, e.x, e.y), t[0] = e.x, t[1] = e.y);
  else {
    if (Ct(t, e)) return;
    r.uniform2fv(this.addr, e), Lt(t, e);
  }
}
function Fv(r, e) {
  const t = this.cache;
  if (e.x !== void 0) (t[0] !== e.x || t[1] !== e.y || t[2] !== e.z) && (r.uniform3f(this.addr, e.x, e.y, e.z), t[0] = e.x, t[1] = e.y, t[2] = e.z);
  else if (e.r !== void 0) (t[0] !== e.r || t[1] !== e.g || t[2] !== e.b) && (r.uniform3f(this.addr, e.r, e.g, e.b), t[0] = e.r, t[1] = e.g, t[2] = e.b);
  else {
    if (Ct(t, e)) return;
    r.uniform3fv(this.addr, e), Lt(t, e);
  }
}
function Bv(r, e) {
  const t = this.cache;
  if (e.x !== void 0) (t[0] !== e.x || t[1] !== e.y || t[2] !== e.z || t[3] !== e.w) && (r.uniform4f(this.addr, e.x, e.y, e.z, e.w), t[0] = e.x, t[1] = e.y, t[2] = e.z, t[3] = e.w);
  else {
    if (Ct(t, e)) return;
    r.uniform4fv(this.addr, e), Lt(t, e);
  }
}
function Vv(r, e) {
  const t = this.cache, i = e.elements;
  if (i === void 0) {
    if (Ct(t, e)) return;
    r.uniformMatrix2fv(this.addr, !1, e), Lt(t, e);
  } else {
    if (Ct(t, i)) return;
    ch.set(i), r.uniformMatrix2fv(this.addr, !1, ch), Lt(t, i);
  }
}
function Hv(r, e) {
  const t = this.cache, i = e.elements;
  if (i === void 0) {
    if (Ct(t, e)) return;
    r.uniformMatrix3fv(this.addr, !1, e), Lt(t, e);
  } else {
    if (Ct(t, i)) return;
    lh.set(i), r.uniformMatrix3fv(this.addr, !1, lh), Lt(t, i);
  }
}
function kv(r, e) {
  const t = this.cache, i = e.elements;
  if (i === void 0) {
    if (Ct(t, e)) return;
    r.uniformMatrix4fv(this.addr, !1, e), Lt(t, e);
  } else {
    if (Ct(t, i)) return;
    oh.set(i), r.uniformMatrix4fv(this.addr, !1, oh), Lt(t, i);
  }
}
function zv(r, e) {
  const t = this.cache;
  t[0] !== e && (r.uniform1i(this.addr, e), t[0] = e);
}
function Gv(r, e) {
  const t = this.cache;
  if (e.x !== void 0) (t[0] !== e.x || t[1] !== e.y) && (r.uniform2i(this.addr, e.x, e.y), t[0] = e.x, t[1] = e.y);
  else {
    if (Ct(t, e)) return;
    r.uniform2iv(this.addr, e), Lt(t, e);
  }
}
function Wv(r, e) {
  const t = this.cache;
  if (e.x !== void 0) (t[0] !== e.x || t[1] !== e.y || t[2] !== e.z) && (r.uniform3i(this.addr, e.x, e.y, e.z), t[0] = e.x, t[1] = e.y, t[2] = e.z);
  else {
    if (Ct(t, e)) return;
    r.uniform3iv(this.addr, e), Lt(t, e);
  }
}
function Xv(r, e) {
  const t = this.cache;
  if (e.x !== void 0) (t[0] !== e.x || t[1] !== e.y || t[2] !== e.z || t[3] !== e.w) && (r.uniform4i(this.addr, e.x, e.y, e.z, e.w), t[0] = e.x, t[1] = e.y, t[2] = e.z, t[3] = e.w);
  else {
    if (Ct(t, e)) return;
    r.uniform4iv(this.addr, e), Lt(t, e);
  }
}
function jv(r, e) {
  const t = this.cache;
  t[0] !== e && (r.uniform1ui(this.addr, e), t[0] = e);
}
function qv(r, e) {
  const t = this.cache;
  if (e.x !== void 0) (t[0] !== e.x || t[1] !== e.y) && (r.uniform2ui(this.addr, e.x, e.y), t[0] = e.x, t[1] = e.y);
  else {
    if (Ct(t, e)) return;
    r.uniform2uiv(this.addr, e), Lt(t, e);
  }
}
function Yv(r, e) {
  const t = this.cache;
  if (e.x !== void 0) (t[0] !== e.x || t[1] !== e.y || t[2] !== e.z) && (r.uniform3ui(this.addr, e.x, e.y, e.z), t[0] = e.x, t[1] = e.y, t[2] = e.z);
  else {
    if (Ct(t, e)) return;
    r.uniform3uiv(this.addr, e), Lt(t, e);
  }
}
function Kv(r, e) {
  const t = this.cache;
  if (e.x !== void 0) (t[0] !== e.x || t[1] !== e.y || t[2] !== e.z || t[3] !== e.w) && (r.uniform4ui(this.addr, e.x, e.y, e.z, e.w), t[0] = e.x, t[1] = e.y, t[2] = e.z, t[3] = e.w);
  else {
    if (Ct(t, e)) return;
    r.uniform4uiv(this.addr, e), Lt(t, e);
  }
}
function Zv(r, e, t) {
  const i = this.cache, n = t.allocateTextureUnit();
  i[0] !== n && (r.uniform1i(this.addr, n), i[0] = n);
  let s;
  this.type === r.SAMPLER_2D_SHADOW ? (Sl.compareFunction = t.isReversedDepthBuffer() ? Wl : Gl, s = Sl) : s = gd, t.setTexture2D(e || s, n);
}
function $v(r, e, t) {
  const i = this.cache, n = t.allocateTextureUnit();
  i[0] !== n && (r.uniform1i(this.addr, n), i[0] = n), t.setTexture3D(e || vd, n);
}
function Qv(r, e, t) {
  const i = this.cache, n = t.allocateTextureUnit();
  i[0] !== n && (r.uniform1i(this.addr, n), i[0] = n), t.setTextureCube(e || xd, n);
}
function Jv(r, e, t) {
  const i = this.cache, n = t.allocateTextureUnit();
  i[0] !== n && (r.uniform1i(this.addr, n), i[0] = n), t.setTexture2DArray(e || _d, n);
}
function e0(r) {
  switch (r) {
    case 5126:
      return Dv;
    case 35664:
      return Ov;
    case 35665:
      return Fv;
    case 35666:
      return Bv;
    case 35674:
      return Vv;
    case 35675:
      return Hv;
    case 35676:
      return kv;
    case 5124:
    case 35670:
      return zv;
    case 35667:
    case 35671:
      return Gv;
    case 35668:
    case 35672:
      return Wv;
    case 35669:
    case 35673:
      return Xv;
    case 5125:
      return jv;
    case 36294:
      return qv;
    case 36295:
      return Yv;
    case 36296:
      return Kv;
    case 35678:
    case 36198:
    case 36298:
    case 36306:
    case 35682:
      return Zv;
    case 35679:
    case 36299:
    case 36307:
      return $v;
    case 35680:
    case 36300:
    case 36308:
    case 36293:
      return Qv;
    case 36289:
    case 36303:
    case 36311:
    case 36292:
      return Jv;
  }
}
function t0(r, e) {
  r.uniform1fv(this.addr, e);
}
function i0(r, e) {
  const t = bn(e, this.size, 2);
  r.uniform2fv(this.addr, t);
}
function r0(r, e) {
  const t = bn(e, this.size, 3);
  r.uniform3fv(this.addr, t);
}
function n0(r, e) {
  const t = bn(e, this.size, 4);
  r.uniform4fv(this.addr, t);
}
function s0(r, e) {
  const t = bn(e, this.size, 4);
  r.uniformMatrix2fv(this.addr, !1, t);
}
function a0(r, e) {
  const t = bn(e, this.size, 9);
  r.uniformMatrix3fv(this.addr, !1, t);
}
function o0(r, e) {
  const t = bn(e, this.size, 16);
  r.uniformMatrix4fv(this.addr, !1, t);
}
function l0(r, e) {
  r.uniform1iv(this.addr, e);
}
function c0(r, e) {
  r.uniform2iv(this.addr, e);
}
function h0(r, e) {
  r.uniform3iv(this.addr, e);
}
function u0(r, e) {
  r.uniform4iv(this.addr, e);
}
function d0(r, e) {
  r.uniform1uiv(this.addr, e);
}
function p0(r, e) {
  r.uniform2uiv(this.addr, e);
}
function f0(r, e) {
  r.uniform3uiv(this.addr, e);
}
function m0(r, e) {
  r.uniform4uiv(this.addr, e);
}
function g0(r, e, t) {
  const i = this.cache, n = e.length, s = wa(t, n);
  Ct(i, s) || (r.uniform1iv(this.addr, s), Lt(i, s));
  let a;
  this.type === r.SAMPLER_2D_SHADOW ? a = Sl : a = gd;
  for (let o = 0; o !== n; ++o) t.setTexture2D(e[o] || a, s[o]);
}
function _0(r, e, t) {
  const i = this.cache, n = e.length, s = wa(t, n);
  Ct(i, s) || (r.uniform1iv(this.addr, s), Lt(i, s));
  for (let a = 0; a !== n; ++a) t.setTexture3D(e[a] || vd, s[a]);
}
function v0(r, e, t) {
  const i = this.cache, n = e.length, s = wa(t, n);
  Ct(i, s) || (r.uniform1iv(this.addr, s), Lt(i, s));
  for (let a = 0; a !== n; ++a) t.setTextureCube(e[a] || xd, s[a]);
}
function x0(r, e, t) {
  const i = this.cache, n = e.length, s = wa(t, n);
  Ct(i, s) || (r.uniform1iv(this.addr, s), Lt(i, s));
  for (let a = 0; a !== n; ++a) t.setTexture2DArray(e[a] || _d, s[a]);
}
function M0(r) {
  switch (r) {
    case 5126:
      return t0;
    case 35664:
      return i0;
    case 35665:
      return r0;
    case 35666:
      return n0;
    case 35674:
      return s0;
    case 35675:
      return a0;
    case 35676:
      return o0;
    case 5124:
    case 35670:
      return l0;
    case 35667:
    case 35671:
      return c0;
    case 35668:
    case 35672:
      return h0;
    case 35669:
    case 35673:
      return u0;
    case 5125:
      return d0;
    case 36294:
      return p0;
    case 36295:
      return f0;
    case 36296:
      return m0;
    case 35678:
    case 36198:
    case 36298:
    case 36306:
    case 35682:
      return g0;
    case 35679:
    case 36299:
    case 36307:
      return _0;
    case 35680:
    case 36300:
    case 36308:
    case 36293:
      return v0;
    case 36289:
    case 36303:
    case 36311:
    case 36292:
      return x0;
  }
}
class y0 {
  constructor(e, t, i) {
    this.id = e, this.addr = i, this.cache = [], this.type = t.type, this.setValue = e0(t.type);
  }
}
class S0 {
  constructor(e, t, i) {
    this.id = e, this.addr = i, this.cache = [], this.type = t.type, this.size = t.size, this.setValue = M0(t.type);
  }
}
class T0 {
  constructor(e) {
    this.id = e, this.seq = [], this.map = {};
  }
  setValue(e, t, i) {
    const n = this.seq;
    for (let s = 0, a = n.length; s !== a; ++s) {
      const o = n[s];
      o.setValue(e, t[o.id], i);
    }
  }
}
const ho = /(\w+)(\])?(\[|\.)?/g;
function hh(r, e) {
  r.seq.push(e), r.map[e.id] = e;
}
function E0(r, e, t) {
  const i = r.name, n = i.length;
  for (ho.lastIndex = 0; ; ) {
    const s = ho.exec(i), a = ho.lastIndex;
    let o = s[1];
    const l = s[2] === "]", c = s[3];
    if (l && (o = o | 0), c === void 0 || c === "[" && a + 2 === n) {
      hh(t, c === void 0 ? new y0(o, r, e) : new S0(o, r, e));
      break;
    } else {
      let h = t.map[o];
      h === void 0 && (h = new T0(o), hh(t, h)), t = h;
    }
  }
}
class ea {
  constructor(e, t) {
    this.seq = [], this.map = {};
    const i = e.getProgramParameter(t, e.ACTIVE_UNIFORMS);
    for (let a = 0; a < i; ++a) {
      const o = e.getActiveUniform(t, a), l = e.getUniformLocation(t, o.name);
      E0(o, l, this);
    }
    const n = [], s = [];
    for (const a of this.seq) a.type === e.SAMPLER_2D_SHADOW || a.type === e.SAMPLER_CUBE_SHADOW || a.type === e.SAMPLER_2D_ARRAY_SHADOW ? n.push(a) : s.push(a);
    n.length > 0 && (this.seq = n.concat(s));
  }
  setValue(e, t, i, n) {
    const s = this.map[t];
    s !== void 0 && s.setValue(e, i, n);
  }
  setOptional(e, t, i) {
    const n = t[i];
    n !== void 0 && this.setValue(e, i, n);
  }
  static upload(e, t, i, n) {
    for (let s = 0, a = t.length; s !== a; ++s) {
      const o = t[s], l = i[o.id];
      l.needsUpdate !== !1 && o.setValue(e, l.value, n);
    }
  }
  static seqWithValue(e, t) {
    const i = [];
    for (let n = 0, s = e.length; n !== s; ++n) {
      const a = e[n];
      a.id in t && i.push(a);
    }
    return i;
  }
}
function uh(r, e, t) {
  const i = r.createShader(e);
  return r.shaderSource(i, t), r.compileShader(i), i;
}
const b0 = 37297;
let w0 = 0;
function A0(r, e) {
  const t = r.split(`
`), i = [], n = Math.max(e - 6, 0), s = Math.min(e + 6, t.length);
  for (let a = n; a < s; a++) {
    const o = a + 1;
    i.push(`${o === e ? ">" : " "} ${o}: ${t[a]}`);
  }
  return i.join(`
`);
}
const dh = new De();
function R0(r) {
  Ze._getMatrix(dh, Ze.workingColorSpace, r);
  const e = `mat3( ${dh.elements.map((t) => t.toFixed(4))} )`;
  switch (Ze.getTransfer(r)) {
    case ca:
      return [e, "LinearTransferOETF"];
    case ot:
      return [e, "sRGBTransferOETF"];
    default:
      return Re("WebGLProgram: Unsupported color space: ", r), [e, "LinearTransferOETF"];
  }
}
function ph(r, e, t) {
  const i = r.getShaderParameter(e, r.COMPILE_STATUS), n = (r.getShaderInfoLog(e) || "").trim();
  if (i && n === "") return "";
  const s = /ERROR: 0:(\d+)/.exec(n);
  if (s) {
    const a = parseInt(s[1]);
    return t.toUpperCase() + `

` + n + `

` + A0(r.getShaderSource(e), a);
  } else return n;
}
function P0(r, e) {
  const t = R0(e);
  return [`vec4 ${r}( vec4 value ) {`, `	return ${t[1]}( vec4( value.rgb * ${t[0]}, value.a ) );`, "}"].join(`
`);
}
const C0 = { [Eu]: "Linear", [bu]: "Reinhard", [wu]: "Cineon", [Au]: "ACESFilmic", [Pu]: "AgX", [Cu]: "Neutral", [Ru]: "Custom" };
function L0(r, e) {
  const t = C0[e];
  return t === void 0 ? (Re("WebGLProgram: Unsupported toneMapping:", e), "vec3 " + r + "( vec3 color ) { return LinearToneMapping( color ); }") : "vec3 " + r + "( vec3 color ) { return " + t + "ToneMapping( color ); }";
}
const zs = new P();
function I0() {
  Ze.getLuminanceCoefficients(zs);
  const r = zs.x.toFixed(4), e = zs.y.toFixed(4), t = zs.z.toFixed(4);
  return ["float luminance( const in vec3 rgb ) {", `	const vec3 weights = vec3( ${r}, ${e}, ${t} );`, "	return dot( weights, rgb );", "}"].join(`
`);
}
function N0(r) {
  return [r.extensionClipCullDistance ? "#extension GL_ANGLE_clip_cull_distance : require" : "", r.extensionMultiDraw ? "#extension GL_ANGLE_multi_draw : require" : ""].filter(Zn).join(`
`);
}
function U0(r) {
  const e = [];
  for (const t in r) {
    const i = r[t];
    i !== !1 && e.push("#define " + t + " " + i);
  }
  return e.join(`
`);
}
function D0(r, e) {
  const t = {}, i = r.getProgramParameter(e, r.ACTIVE_ATTRIBUTES);
  for (let n = 0; n < i; n++) {
    const s = r.getActiveAttrib(e, n), a = s.name;
    let o = 1;
    s.type === r.FLOAT_MAT2 && (o = 2), s.type === r.FLOAT_MAT3 && (o = 3), s.type === r.FLOAT_MAT4 && (o = 4), t[a] = { type: s.type, location: r.getAttribLocation(e, a), locationSize: o };
  }
  return t;
}
function Zn(r) {
  return r !== "";
}
function fh(r, e) {
  const t = e.numSpotLightShadows + e.numSpotLightMaps - e.numSpotLightShadowsWithMaps;
  return r.replace(/NUM_SUN_LIGHTS/g, e.numSunLights).replace(/NUM_DIR_LIGHTS/g, e.numDirLights).replace(/NUM_SPOT_LIGHTS/g, e.numSpotLights).replace(/NUM_SPOT_LIGHT_MAPS/g, e.numSpotLightMaps).replace(/NUM_SPOT_LIGHT_COORDS/g, t).replace(/NUM_RECT_AREA_LIGHTS/g, e.numRectAreaLights).replace(/NUM_POINT_LIGHTS/g, e.numPointLights).replace(/NUM_HEMI_LIGHTS/g, e.numHemiLights).replace(/NUM_SUN_LIGHT_SHADOWS/g, e.numSunLightShadows).replace(/NUM_DIR_LIGHT_SHADOWS/g, e.numDirLightShadows).replace(/NUM_SPOT_LIGHT_SHADOWS_WITH_MAPS/g, e.numSpotLightShadowsWithMaps).replace(/NUM_SPOT_LIGHT_SHADOWS/g, e.numSpotLightShadows).replace(/NUM_POINT_LIGHT_SHADOWS/g, e.numPointLightShadows);
}
function mh(r, e) {
  return r.replace(/NUM_CLIPPING_PLANES/g, e.numClippingPlanes).replace(/UNION_CLIPPING_PLANES/g, e.numClippingPlanes - e.numClipIntersection);
}
const O0 = /^[ \t]*#include +<([\w\d./]+)>/gm;
function Tl(r) {
  return r.replace(O0, B0);
}
const F0 = /* @__PURE__ */ new Map();
function B0(r, e) {
  let t = Ge[e];
  if (t === void 0) {
    const i = F0.get(e);
    if (i !== void 0) t = Ge[i], Re('WebGLRenderer: Shader chunk "%s" has been deprecated. Use "%s" instead.', e, i);
    else throw new Error("THREE.WebGLProgram: Can not resolve #include <" + e + ">");
  }
  return Tl(t);
}
const V0 = /#pragma unroll_loop_start\s+for\s*\(\s*int\s+i\s*=\s*(\d+)\s*;\s*i\s*<\s*(\d+)\s*;\s*i\s*\+\+\s*\)\s*{([\s\S]+?)}\s+#pragma unroll_loop_end/g;
function gh(r) {
  return r.replace(V0, H0);
}
function H0(r, e, t, i) {
  let n = "";
  for (let s = parseInt(e); s < parseInt(t); s++) n += i.replace(/\[\s*i\s*\]/g, "[ " + s + " ]").replace(/UNROLLED_LOOP_INDEX/g, s);
  return n;
}
function _h(r) {
  let e = `precision ${r.precision} float;
	precision ${r.precision} int;
	precision ${r.precision} sampler2D;
	precision ${r.precision} samplerCube;
	precision ${r.precision} sampler3D;
	precision ${r.precision} sampler2DArray;
	precision ${r.precision} sampler2DShadow;
	precision ${r.precision} samplerCubeShadow;
	precision ${r.precision} sampler2DArrayShadow;
	precision ${r.precision} isampler2D;
	precision ${r.precision} isampler3D;
	precision ${r.precision} isamplerCube;
	precision ${r.precision} isampler2DArray;
	precision ${r.precision} usampler2D;
	precision ${r.precision} usampler3D;
	precision ${r.precision} usamplerCube;
	precision ${r.precision} usampler2DArray;
	`;
  return r.precision === "highp" ? e += `
#define HIGH_PRECISION` : r.precision === "mediump" ? e += `
#define MEDIUM_PRECISION` : r.precision === "lowp" && (e += `
#define LOW_PRECISION`), e;
}
const k0 = { [qs]: "SHADOWMAP_TYPE_PCF", [qn]: "SHADOWMAP_TYPE_VSM" };
function z0(r) {
  return k0[r.shadowMapType] || "SHADOWMAP_TYPE_BASIC";
}
const G0 = { [Nr]: "ENVMAP_TYPE_CUBE", [gn]: "ENVMAP_TYPE_CUBE", [xa]: "ENVMAP_TYPE_CUBE_UV" };
function W0(r) {
  return r.envMap === !1 ? "ENVMAP_TYPE_CUBE" : G0[r.envMapMode] || "ENVMAP_TYPE_CUBE";
}
const X0 = { [gn]: "ENVMAP_MODE_REFRACTION" };
function j0(r) {
  return r.envMap === !1 ? "ENVMAP_MODE_REFLECTION" : X0[r.envMapMode] || "ENVMAP_MODE_REFLECTION";
}
const q0 = { [Tu]: "ENVMAP_BLENDING_MULTIPLY", [Pp]: "ENVMAP_BLENDING_MIX", [Cp]: "ENVMAP_BLENDING_ADD" };
function Y0(r) {
  return r.envMap === !1 ? "ENVMAP_BLENDING_NONE" : q0[r.combine] || "ENVMAP_BLENDING_NONE";
}
function K0(r) {
  const e = r.envMapCubeUVHeight;
  if (e === null) return null;
  const t = Math.log2(e) - 2, i = 1 / e;
  return { texelWidth: 1 / (3 * Math.max(Math.pow(2, t), 7 * 16)), texelHeight: i, maxMip: t };
}
function Z0(r, e, t, i) {
  const n = r.getContext(), s = t.defines;
  let a = t.vertexShader, o = t.fragmentShader;
  const l = z0(t), c = W0(t), h = j0(t), d = Y0(t), u = K0(t), p = N0(t), f = U0(s), v = n.createProgram();
  let m, g, E = t.glslVersion ? "#version " + t.glslVersion + `
` : "";
  t.isRawShaderMaterial ? (m = ["#define SHADER_TYPE " + t.shaderType, "#define SHADER_NAME " + t.shaderName, f].filter(Zn).join(`
`), m.length > 0 && (m += `
`), g = ["#define SHADER_TYPE " + t.shaderType, "#define SHADER_NAME " + t.shaderName, f].filter(Zn).join(`
`), g.length > 0 && (g += `
`)) : (m = [_h(t), "#define SHADER_TYPE " + t.shaderType, "#define SHADER_NAME " + t.shaderName, f, t.extensionClipCullDistance ? "#define USE_CLIP_DISTANCE" : "", t.batching ? "#define USE_BATCHING" : "", t.batchingColor ? "#define USE_BATCHING_COLOR" : "", t.instancing ? "#define USE_INSTANCING" : "", t.instancingColor ? "#define USE_INSTANCING_COLOR" : "", t.instancingMorph ? "#define USE_INSTANCING_MORPH" : "", t.useFog && t.fog ? "#define USE_FOG" : "", t.useFog && t.fogExp2 ? "#define FOG_EXP2" : "", t.map ? "#define USE_MAP" : "", t.envMap ? "#define USE_ENVMAP" : "", t.envMap ? "#define " + h : "", t.lightMap ? "#define USE_LIGHTMAP" : "", t.aoMap ? "#define USE_AOMAP" : "", t.bumpMap ? "#define USE_BUMPMAP" : "", t.normalMap ? "#define USE_NORMALMAP" : "", t.normalMapObjectSpace ? "#define USE_NORMALMAP_OBJECTSPACE" : "", t.normalMapTangentSpace ? "#define USE_NORMALMAP_TANGENTSPACE" : "", t.displacementMap ? "#define USE_DISPLACEMENTMAP" : "", t.emissiveMap ? "#define USE_EMISSIVEMAP" : "", t.anisotropy ? "#define USE_ANISOTROPY" : "", t.anisotropyMap ? "#define USE_ANISOTROPYMAP" : "", t.clearcoatMap ? "#define USE_CLEARCOATMAP" : "", t.clearcoatRoughnessMap ? "#define USE_CLEARCOAT_ROUGHNESSMAP" : "", t.clearcoatNormalMap ? "#define USE_CLEARCOAT_NORMALMAP" : "", t.iridescenceMap ? "#define USE_IRIDESCENCEMAP" : "", t.iridescenceThicknessMap ? "#define USE_IRIDESCENCE_THICKNESSMAP" : "", t.specularMap ? "#define USE_SPECULARMAP" : "", t.specularColorMap ? "#define USE_SPECULAR_COLORMAP" : "", t.specularIntensityMap ? "#define USE_SPECULAR_INTENSITYMAP" : "", t.roughnessMap ? "#define USE_ROUGHNESSMAP" : "", t.metalnessMap ? "#define USE_METALNESSMAP" : "", t.alphaMap ? "#define USE_ALPHAMAP" : "", t.alphaHash ? "#define USE_ALPHAHASH" : "", t.transmission ? "#define USE_TRANSMISSION" : "", t.transmissionMap ? "#define USE_TRANSMISSIONMAP" : "", t.thicknessMap ? "#define USE_THICKNESSMAP" : "", t.sheenColorMap ? "#define USE_SHEEN_COLORMAP" : "", t.sheenRoughnessMap ? "#define USE_SHEEN_ROUGHNESSMAP" : "", t.mapUv ? "#define MAP_UV " + t.mapUv : "", t.alphaMapUv ? "#define ALPHAMAP_UV " + t.alphaMapUv : "", t.lightMapUv ? "#define LIGHTMAP_UV " + t.lightMapUv : "", t.aoMapUv ? "#define AOMAP_UV " + t.aoMapUv : "", t.emissiveMapUv ? "#define EMISSIVEMAP_UV " + t.emissiveMapUv : "", t.bumpMapUv ? "#define BUMPMAP_UV " + t.bumpMapUv : "", t.normalMapUv ? "#define NORMALMAP_UV " + t.normalMapUv : "", t.displacementMapUv ? "#define DISPLACEMENTMAP_UV " + t.displacementMapUv : "", t.metalnessMapUv ? "#define METALNESSMAP_UV " + t.metalnessMapUv : "", t.roughnessMapUv ? "#define ROUGHNESSMAP_UV " + t.roughnessMapUv : "", t.anisotropyMapUv ? "#define ANISOTROPYMAP_UV " + t.anisotropyMapUv : "", t.clearcoatMapUv ? "#define CLEARCOATMAP_UV " + t.clearcoatMapUv : "", t.clearcoatNormalMapUv ? "#define CLEARCOAT_NORMALMAP_UV " + t.clearcoatNormalMapUv : "", t.clearcoatRoughnessMapUv ? "#define CLEARCOAT_ROUGHNESSMAP_UV " + t.clearcoatRoughnessMapUv : "", t.iridescenceMapUv ? "#define IRIDESCENCEMAP_UV " + t.iridescenceMapUv : "", t.iridescenceThicknessMapUv ? "#define IRIDESCENCE_THICKNESSMAP_UV " + t.iridescenceThicknessMapUv : "", t.sheenColorMapUv ? "#define SHEEN_COLORMAP_UV " + t.sheenColorMapUv : "", t.sheenRoughnessMapUv ? "#define SHEEN_ROUGHNESSMAP_UV " + t.sheenRoughnessMapUv : "", t.specularMapUv ? "#define SPECULARMAP_UV " + t.specularMapUv : "", t.specularColorMapUv ? "#define SPECULAR_COLORMAP_UV " + t.specularColorMapUv : "", t.specularIntensityMapUv ? "#define SPECULAR_INTENSITYMAP_UV " + t.specularIntensityMapUv : "", t.transmissionMapUv ? "#define TRANSMISSIONMAP_UV " + t.transmissionMapUv : "", t.thicknessMapUv ? "#define THICKNESSMAP_UV " + t.thicknessMapUv : "", t.vertexTangents && t.flatShading === !1 ? "#define USE_TANGENT" : "", t.vertexNormals ? "#define HAS_NORMAL" : "", t.vertexColors ? "#define USE_COLOR" : "", t.vertexAlphas ? "#define USE_COLOR_ALPHA" : "", t.vertexUv1s ? "#define USE_UV1" : "", t.vertexUv2s ? "#define USE_UV2" : "", t.vertexUv3s ? "#define USE_UV3" : "", t.pointsUvs ? "#define USE_POINTS_UV" : "", t.flatShading ? "#define FLAT_SHADED" : "", t.skinning ? "#define USE_SKINNING" : "", t.morphTargets ? "#define USE_MORPHTARGETS" : "", t.morphNormals && t.flatShading === !1 ? "#define USE_MORPHNORMALS" : "", t.morphColors ? "#define USE_MORPHCOLORS" : "", t.morphTargetsCount > 0 ? "#define MORPHTARGETS_TEXTURE_STRIDE " + t.morphTextureStride : "", t.morphTargetsCount > 0 ? "#define MORPHTARGETS_COUNT " + t.morphTargetsCount : "", t.doubleSided ? "#define DOUBLE_SIDED" : "", t.flipSided ? "#define FLIP_SIDED" : "", t.shadowMapEnabled ? "#define USE_SHADOWMAP" : "", t.shadowMapEnabled ? "#define " + l : "", t.sizeAttenuation ? "#define USE_SIZEATTENUATION" : "", t.numLightProbes > 0 ? "#define USE_LIGHT_PROBES" : "", t.logarithmicDepthBuffer ? "#define USE_LOGARITHMIC_DEPTH_BUFFER" : "", t.reversedDepthBuffer ? "#define USE_REVERSED_DEPTH_BUFFER" : "", "uniform mat4 modelMatrix;", "uniform mat4 modelViewMatrix;", "uniform mat4 projectionMatrix;", "uniform mat4 viewMatrix;", "uniform mat3 normalMatrix;", "uniform vec3 cameraPosition;", "uniform bool isOrthographic;", "#ifdef USE_INSTANCING", "	attribute mat4 instanceMatrix;", "#endif", "#ifdef USE_INSTANCING_COLOR", "	attribute vec3 instanceColor;", "#endif", "#ifdef USE_INSTANCING_MORPH", "	uniform sampler2D morphTexture;", "#endif", "attribute vec3 position;", "attribute vec3 normal;", "attribute vec2 uv;", "#ifdef USE_UV1", "	attribute vec2 uv1;", "#endif", "#ifdef USE_UV2", "	attribute vec2 uv2;", "#endif", "#ifdef USE_UV3", "	attribute vec2 uv3;", "#endif", "#ifdef USE_TANGENT", "	attribute vec4 tangent;", "#endif", "#if defined( USE_COLOR_ALPHA )", "	attribute vec4 color;", "#elif defined( USE_COLOR )", "	attribute vec3 color;", "#endif", "#ifdef USE_SKINNING", "	attribute vec4 skinIndex;", "	attribute vec4 skinWeight;", "#endif", `
`].filter(Zn).join(`
`), g = [_h(t), "#define SHADER_TYPE " + t.shaderType, "#define SHADER_NAME " + t.shaderName, f, t.useFog && t.fog ? "#define USE_FOG" : "", t.useFog && t.fogExp2 ? "#define FOG_EXP2" : "", t.alphaToCoverage ? "#define ALPHA_TO_COVERAGE" : "", t.map ? "#define USE_MAP" : "", t.matcap ? "#define USE_MATCAP" : "", t.envMap ? "#define USE_ENVMAP" : "", t.envMap ? "#define " + c : "", t.envMap ? "#define " + h : "", t.envMap ? "#define " + d : "", u ? "#define CUBEUV_TEXEL_WIDTH " + u.texelWidth : "", u ? "#define CUBEUV_TEXEL_HEIGHT " + u.texelHeight : "", u ? "#define CUBEUV_MAX_MIP " + u.maxMip + ".0" : "", t.lightMap ? "#define USE_LIGHTMAP" : "", t.aoMap ? "#define USE_AOMAP" : "", t.bumpMap ? "#define USE_BUMPMAP" : "", t.normalMap ? "#define USE_NORMALMAP" : "", t.normalMapObjectSpace ? "#define USE_NORMALMAP_OBJECTSPACE" : "", t.normalMapTangentSpace ? "#define USE_NORMALMAP_TANGENTSPACE" : "", t.packedNormalMap ? "#define USE_PACKED_NORMALMAP" : "", t.emissiveMap ? "#define USE_EMISSIVEMAP" : "", t.anisotropy ? "#define USE_ANISOTROPY" : "", t.anisotropyMap ? "#define USE_ANISOTROPYMAP" : "", t.clearcoat ? "#define USE_CLEARCOAT" : "", t.clearcoatMap ? "#define USE_CLEARCOATMAP" : "", t.clearcoatRoughnessMap ? "#define USE_CLEARCOAT_ROUGHNESSMAP" : "", t.clearcoatNormalMap ? "#define USE_CLEARCOAT_NORMALMAP" : "", t.dispersion ? "#define USE_DISPERSION" : "", t.retroreflection ? "#define USE_RETROREFLECTION" : "", t.iridescence ? "#define USE_IRIDESCENCE" : "", t.iridescenceMap ? "#define USE_IRIDESCENCEMAP" : "", t.iridescenceThicknessMap ? "#define USE_IRIDESCENCE_THICKNESSMAP" : "", t.specularMap ? "#define USE_SPECULARMAP" : "", t.specularColorMap ? "#define USE_SPECULAR_COLORMAP" : "", t.specularIntensityMap ? "#define USE_SPECULAR_INTENSITYMAP" : "", t.roughnessMap ? "#define USE_ROUGHNESSMAP" : "", t.metalnessMap ? "#define USE_METALNESSMAP" : "", t.alphaMap ? "#define USE_ALPHAMAP" : "", t.alphaTest ? "#define USE_ALPHATEST" : "", t.alphaHash ? "#define USE_ALPHAHASH" : "", t.sheen ? "#define USE_SHEEN" : "", t.sheenColorMap ? "#define USE_SHEEN_COLORMAP" : "", t.sheenRoughnessMap ? "#define USE_SHEEN_ROUGHNESSMAP" : "", t.transmission ? "#define USE_TRANSMISSION" : "", t.transmissionMap ? "#define USE_TRANSMISSIONMAP" : "", t.thicknessMap ? "#define USE_THICKNESSMAP" : "", t.vertexTangents && t.flatShading === !1 ? "#define USE_TANGENT" : "", t.vertexColors || t.instancingColor ? "#define USE_COLOR" : "", t.vertexAlphas || t.batchingColor ? "#define USE_COLOR_ALPHA" : "", t.vertexUv1s ? "#define USE_UV1" : "", t.vertexUv2s ? "#define USE_UV2" : "", t.vertexUv3s ? "#define USE_UV3" : "", t.pointsUvs ? "#define USE_POINTS_UV" : "", t.gradientMap ? "#define USE_GRADIENTMAP" : "", t.flatShading ? "#define FLAT_SHADED" : "", t.doubleSided ? "#define DOUBLE_SIDED" : "", t.flipSided ? "#define FLIP_SIDED" : "", t.shadowMapEnabled ? "#define USE_SHADOWMAP" : "", t.shadowMapEnabled ? "#define " + l : "", t.premultipliedAlpha ? "#define PREMULTIPLIED_ALPHA" : "", t.numLightProbes > 0 ? "#define USE_LIGHT_PROBES" : "", t.numLightProbeGrids > 0 ? "#define USE_LIGHT_PROBES_GRID" : "", t.decodeVideoTexture ? "#define DECODE_VIDEO_TEXTURE" : "", t.decodeVideoTextureEmissive ? "#define DECODE_VIDEO_TEXTURE_EMISSIVE" : "", t.logarithmicDepthBuffer ? "#define USE_LOGARITHMIC_DEPTH_BUFFER" : "", t.reversedDepthBuffer ? "#define USE_REVERSED_DEPTH_BUFFER" : "", "uniform mat4 viewMatrix;", "uniform vec3 cameraPosition;", "uniform bool isOrthographic;", t.toneMapping !== Ii ? "#define TONE_MAPPING" : "", t.toneMapping !== Ii ? Ge.tonemapping_pars_fragment : "", t.toneMapping !== Ii ? L0("toneMapping", t.toneMapping) : "", t.dithering ? "#define DITHERING" : "", t.opaque ? "#define OPAQUE" : "", Ge.colorspace_pars_fragment, P0("linearToOutputTexel", t.outputColorSpace), I0(), t.useDepthPacking ? "#define DEPTH_PACKING " + t.depthPacking : "", `
`].filter(Zn).join(`
`)), a = Tl(a), a = fh(a, t), a = mh(a, t), o = Tl(o), o = fh(o, t), o = mh(o, t), a = gh(a), o = gh(o), t.isRawShaderMaterial !== !0 && (E = `#version 300 es
`, m = [p, "#define attribute in", "#define varying out", "#define texture2D texture"].join(`
`) + `
` + m, g = ["#define varying in", t.glslVersion === fc ? "" : "layout(location = 0) out highp vec4 pc_fragColor;", t.glslVersion === fc ? "" : "#define gl_FragColor pc_fragColor", "#define gl_FragDepthEXT gl_FragDepth", "#define texture2D texture", "#define textureCube texture", "#define texture2DProj textureProj", "#define texture2DLodEXT textureLod", "#define texture2DProjLodEXT textureProjLod", "#define textureCubeLodEXT textureLod", "#define texture2DGradEXT textureGrad", "#define texture2DProjGradEXT textureProjGrad", "#define textureCubeGradEXT textureGrad"].join(`
`) + `
` + g);
  const R = E + m + a, y = E + g + o, S = uh(n, n.VERTEX_SHADER, R), T = uh(n, n.FRAGMENT_SHADER, y);
  n.attachShader(v, S), n.attachShader(v, T), t.index0AttributeName !== void 0 ? n.bindAttribLocation(v, 0, t.index0AttributeName) : t.hasPositionAttribute === !0 && n.bindAttribLocation(v, 0, "position"), n.linkProgram(v);
  function A(O) {
    if (r.debug.checkShaderErrors) {
      const k = n.getProgramInfoLog(v) || "", q = n.getShaderInfoLog(S) || "", B = n.getShaderInfoLog(T) || "", G = k.trim(), Z = q.trim(), X = B.trim();
      let te = !0, V = !0;
      if (n.getProgramParameter(v, n.LINK_STATUS) === !1) if (te = !1, typeof r.debug.onShaderError == "function") r.debug.onShaderError(n, v, S, T);
      else {
        const K = ph(n, S, "vertex"), ee = ph(n, T, "fragment");
        Be("WebGLProgram: Shader Error " + n.getError() + " - VALIDATE_STATUS " + n.getProgramParameter(v, n.VALIDATE_STATUS) + `

Material Name: ` + O.name + `
Material Type: ` + O.type + `

Program Info Log: ` + G + `
` + K + `
` + ee);
      }
      else G !== "" ? Re("WebGLProgram: Program Info Log:", G) : (Z === "" || X === "") && (V = !1);
      V && (O.diagnostics = { runnable: te, programLog: G, vertexShader: { log: Z, prefix: m }, fragmentShader: { log: X, prefix: g } });
    }
    n.deleteShader(S), n.deleteShader(T), x = new ea(n, v), b = D0(n, v);
  }
  let x;
  this.getUniforms = function() {
    return x === void 0 && A(this), x;
  };
  let b;
  this.getAttributes = function() {
    return b === void 0 && A(this), b;
  };
  let N = t.rendererExtensionParallelShaderCompile === !1;
  return this.isReady = function() {
    return N === !1 && (N = n.getProgramParameter(v, b0)), N;
  }, this.destroy = function() {
    i.releaseStatesOfProgram(this), n.deleteProgram(v), this.program = void 0;
  }, this.type = t.shaderType, this.name = t.shaderName, this.id = w0++, this.cacheKey = e, this.usedTimes = 1, this.program = v, this.vertexShader = S, this.fragmentShader = T, this;
}
let $0 = 0;
class Q0 {
  constructor() {
    this.shaderCache = /* @__PURE__ */ new Map(), this.materialCache = /* @__PURE__ */ new Map();
  }
  update(e, t, i) {
    const n = this._getShaderCacheForMaterial(e);
    return n.has(t) === !1 && (n.add(t), t.usedTimes++), n.has(i) === !1 && (n.add(i), i.usedTimes++), this;
  }
  remove(e) {
    const t = this.materialCache.get(e);
    for (const i of t) i.usedTimes--, i.usedTimes === 0 && this.shaderCache.delete(i.code);
    return this.materialCache.delete(e), this;
  }
  getVertexShaderStage(e) {
    return this._getShaderStage(e.vertexShader);
  }
  getFragmentShaderStage(e) {
    return this._getShaderStage(e.fragmentShader);
  }
  dispose() {
    this.shaderCache.clear(), this.materialCache.clear();
  }
  _getShaderCacheForMaterial(e) {
    const t = this.materialCache;
    let i = t.get(e);
    return i === void 0 && (i = /* @__PURE__ */ new Set(), t.set(e, i)), i;
  }
  _getShaderStage(e) {
    const t = this.shaderCache;
    let i = t.get(e);
    return i === void 0 && (i = new J0(e), t.set(e, i)), i;
  }
}
class J0 {
  constructor(e) {
    this.id = $0++, this.code = e, this.usedTimes = 0;
  }
}
function ex(r) {
  return r === Ur || r === sa || r === aa;
}
function tx(r, e, t, i, n, s) {
  const a = new Ku(), o = new Q0(), l = /* @__PURE__ */ new Set(), c = [], h = /* @__PURE__ */ new Map(), d = i.logarithmicDepthBuffer;
  let u = i.precision;
  const p = { MeshDepthMaterial: "depth", MeshDistanceMaterial: "distance", MeshNormalMaterial: "normal", MeshBasicMaterial: "basic", MeshLambertMaterial: "lambert", MeshPhongMaterial: "phong", MeshToonMaterial: "toon", MeshStandardMaterial: "physical", MeshPhysicalMaterial: "physical", MeshMatcapMaterial: "matcap", LineBasicMaterial: "basic", LineDashedMaterial: "dashed", PointsMaterial: "points", ShadowMaterial: "shadow", SpriteMaterial: "sprite" };
  function f(x) {
    return l.add(x), x === 0 ? "uv" : `uv${x}`;
  }
  function v(x, b, N, O, k, q) {
    const B = O.fog, G = k.geometry, Z = x.isMeshStandardMaterial || x.isMeshLambertMaterial || x.isMeshPhongMaterial ? O.environment : null, X = x.isMeshStandardMaterial || x.isMeshLambertMaterial && !x.envMap || x.isMeshPhongMaterial && !x.envMap, te = e.get(x.envMap || Z, X), V = te && te.mapping === xa ? te.image.height : null, K = p[x.type];
    x.precision !== null && (u = i.getMaxPrecision(x.precision), u !== x.precision && Re("WebGLProgram.getParameters:", x.precision, "not supported, using", u, "instead."));
    const ee = G.morphAttributes.position || G.morphAttributes.normal || G.morphAttributes.color, Ue = ee !== void 0 ? ee.length : 0;
    let ue = 0;
    G.morphAttributes.position !== void 0 && (ue = 1), G.morphAttributes.normal !== void 0 && (ue = 2), G.morphAttributes.color !== void 0 && (ue = 3);
    let Ye, ke, je, Y;
    if (K) {
      const Le = Pi[K];
      Ye = Le.vertexShader, ke = Le.fragmentShader;
    } else {
      Ye = x.vertexShader, ke = x.fragmentShader;
      const Le = o.getVertexShaderStage(x), Ne = o.getFragmentShaderStage(x);
      o.update(x, Le, Ne), je = Le.id, Y = Ne.id;
    }
    const $ = r.getRenderTarget(), le = r.state.buffers.depth.getReversed(), Oe = k.isInstancedMesh === !0, xe = k.isBatchedMesh === !0, Te = !!x.map, He = !!x.matcap, Ke = !!te, Ve = !!x.aoMap, vt = !!x.lightMap, Mt = !!x.bumpMap && x.wireframe === !1, dt = !!x.normalMap, Je = !!x.displacementMap, At = !!x.emissiveMap, tt = !!x.metalnessMap, _t = !!x.roughnessMap, C = x.anisotropy > 0, xt = x.clearcoat > 0, Qe = x.dispersion > 0, w = x.retroreflectivity > 0, _ = x.iridescence > 0, F = x.sheen > 0, W = x.transmission > 0, j = C && !!x.anisotropyMap, oe = xt && !!x.clearcoatMap, he = xt && !!x.clearcoatNormalMap, D = xt && !!x.clearcoatRoughnessMap, se = _ && !!x.iridescenceMap, ce = _ && !!x.iridescenceThicknessMap, Me = F && !!x.sheenColorMap, ie = F && !!x.sheenRoughnessMap, ge = !!x.specularMap, Se = !!x.specularColorMap, Pe = !!x.specularIntensityMap, Ie = W && !!x.transmissionMap, U = W && !!x.thicknessMap, Q = !!x.gradientMap, re = !!x.alphaMap, _e = x.alphaTest > 0, ae = !!x.alphaHash, J = !!x.extensions;
    let me = Ii;
    x.toneMapped && ($ === null || $.isXRRenderTarget === !0) && (me = r.toneMapping);
    const pe = { shaderID: K, shaderType: x.type, shaderName: x.name, vertexShader: Ye, fragmentShader: ke, defines: x.defines, customVertexShaderID: je, customFragmentShaderID: Y, isRawShaderMaterial: x.isRawShaderMaterial === !0, glslVersion: x.glslVersion, precision: u, batching: xe, batchingColor: xe && k._colorsTexture !== null, instancing: Oe, instancingColor: Oe && k.instanceColor !== null, instancingMorph: Oe && k.morphTexture !== null, outputColorSpace: $ === null ? r.outputColorSpace : $.isXRRenderTarget === !0 ? $.texture.colorSpace : Ze.workingColorSpace, alphaToCoverage: !!x.alphaToCoverage, map: Te, matcap: He, envMap: Ke, envMapMode: Ke && te.mapping, envMapCubeUVHeight: V, aoMap: Ve, lightMap: vt, bumpMap: Mt, normalMap: dt, displacementMap: Je, emissiveMap: At, normalMapObjectSpace: dt && x.normalMapType === Op, normalMapTangentSpace: dt && x.normalMapType === la, packedNormalMap: dt && x.normalMapType === la && ex(x.normalMap.format), metalnessMap: tt, roughnessMap: _t, anisotropy: C, anisotropyMap: j, clearcoat: xt, clearcoatMap: oe, clearcoatNormalMap: he, clearcoatRoughnessMap: D, dispersion: Qe, retroreflection: w, iridescence: _, iridescenceMap: se, iridescenceThicknessMap: ce, sheen: F, sheenColorMap: Me, sheenRoughnessMap: ie, specularMap: ge, specularColorMap: Se, specularIntensityMap: Pe, transmission: W, transmissionMap: Ie, thicknessMap: U, gradientMap: Q, opaque: x.transparent === !1 && x.blending === Qn && x.alphaToCoverage === !1, alphaMap: re, alphaTest: _e, alphaHash: ae, combine: x.combine, mapUv: Te && f(x.map.channel), aoMapUv: Ve && f(x.aoMap.channel), lightMapUv: vt && f(x.lightMap.channel), bumpMapUv: Mt && f(x.bumpMap.channel), normalMapUv: dt && f(x.normalMap.channel), displacementMapUv: Je && f(x.displacementMap.channel), emissiveMapUv: At && f(x.emissiveMap.channel), metalnessMapUv: tt && f(x.metalnessMap.channel), roughnessMapUv: _t && f(x.roughnessMap.channel), anisotropyMapUv: j && f(x.anisotropyMap.channel), clearcoatMapUv: oe && f(x.clearcoatMap.channel), clearcoatNormalMapUv: he && f(x.clearcoatNormalMap.channel), clearcoatRoughnessMapUv: D && f(x.clearcoatRoughnessMap.channel), iridescenceMapUv: se && f(x.iridescenceMap.channel), iridescenceThicknessMapUv: ce && f(x.iridescenceThicknessMap.channel), sheenColorMapUv: Me && f(x.sheenColorMap.channel), sheenRoughnessMapUv: ie && f(x.sheenRoughnessMap.channel), specularMapUv: ge && f(x.specularMap.channel), specularColorMapUv: Se && f(x.specularColorMap.channel), specularIntensityMapUv: Pe && f(x.specularIntensityMap.channel), transmissionMapUv: Ie && f(x.transmissionMap.channel), thicknessMapUv: U && f(x.thicknessMap.channel), alphaMapUv: re && f(x.alphaMap.channel), vertexTangents: !!G.attributes.tangent && (dt || C), vertexNormals: !!G.attributes.normal, vertexColors: x.vertexColors, vertexAlphas: x.vertexColors === !0 && !!G.attributes.color && G.attributes.color.itemSize === 4, pointsUvs: k.isPoints === !0 && !!G.attributes.uv && (Te || re), fog: !!B, useFog: x.fog === !0, fogExp2: !!B && B.isFogExp2, flatShading: x.wireframe === !1 && (x.flatShading === !0 || G.attributes.normal === void 0 && dt === !1 && (x.isMeshLambertMaterial || x.isMeshPhongMaterial || x.isMeshStandardMaterial || x.isMeshPhysicalMaterial)), sizeAttenuation: x.sizeAttenuation === !0, logarithmicDepthBuffer: d, reversedDepthBuffer: le, skinning: k.isSkinnedMesh === !0, hasPositionAttribute: G.attributes.position !== void 0, morphTargets: G.morphAttributes.position !== void 0, morphNormals: G.morphAttributes.normal !== void 0, morphColors: G.morphAttributes.color !== void 0, morphTargetsCount: Ue, morphTextureStride: ue, numSunLights: b.sun.length, numDirLights: b.directional.length, numPointLights: b.point.length, numSpotLights: b.spot.length, numSpotLightMaps: b.spotLightMap.length, numRectAreaLights: b.rectArea.length, numHemiLights: b.hemi.length, numSunLightShadows: b.sunShadowMap.length, numDirLightShadows: b.directionalShadowMap.length, numPointLightShadows: b.pointShadowMap.length, numSpotLightShadows: b.spotShadowMap.length, numSpotLightShadowsWithMaps: b.numSpotLightShadowsWithMaps, numLightProbes: b.numLightProbes, numLightProbeGrids: q.length, numClippingPlanes: s.numPlanes, numClipIntersection: s.numIntersection, dithering: x.dithering, shadowMapEnabled: r.shadowMap.enabled && N.length > 0, shadowMapType: r.shadowMap.type, toneMapping: me, decodeVideoTexture: Te && x.map.isVideoTexture === !0 && Ze.getTransfer(x.map.colorSpace) === ot, decodeVideoTextureEmissive: At && x.emissiveMap.isVideoTexture === !0 && Ze.getTransfer(x.emissiveMap.colorSpace) === ot, premultipliedAlpha: x.premultipliedAlpha, doubleSided: x.side === ai, flipSided: x.side === Gt, useDepthPacking: x.depthPacking >= 0, depthPacking: x.depthPacking || 0, index0AttributeName: x.index0AttributeName, extensionClipCullDistance: J && x.extensions.clipCullDistance === !0 && t.has("WEBGL_clip_cull_distance"), extensionMultiDraw: (J && x.extensions.multiDraw === !0 || xe) && t.has("WEBGL_multi_draw"), rendererExtensionParallelShaderCompile: t.has("KHR_parallel_shader_compile"), customProgramCacheKey: x.customProgramCacheKey() };
    return pe.vertexUv1s = l.has(1), pe.vertexUv2s = l.has(2), pe.vertexUv3s = l.has(3), l.clear(), pe;
  }
  function m(x) {
    const b = [];
    if (x.shaderID ? b.push(x.shaderID) : (b.push(x.customVertexShaderID), b.push(x.customFragmentShaderID)), x.defines !== void 0) for (const N in x.defines) b.push(N), b.push(x.defines[N]);
    return x.isRawShaderMaterial === !1 && (g(b, x), E(b, x), b.push(r.outputColorSpace)), b.push(x.customProgramCacheKey), b.join();
  }
  function g(x, b) {
    x.push(b.precision), x.push(b.outputColorSpace), x.push(b.envMapMode), x.push(b.envMapCubeUVHeight), x.push(b.mapUv), x.push(b.alphaMapUv), x.push(b.lightMapUv), x.push(b.aoMapUv), x.push(b.bumpMapUv), x.push(b.normalMapUv), x.push(b.displacementMapUv), x.push(b.emissiveMapUv), x.push(b.metalnessMapUv), x.push(b.roughnessMapUv), x.push(b.anisotropyMapUv), x.push(b.clearcoatMapUv), x.push(b.clearcoatNormalMapUv), x.push(b.clearcoatRoughnessMapUv), x.push(b.iridescenceMapUv), x.push(b.iridescenceThicknessMapUv), x.push(b.sheenColorMapUv), x.push(b.sheenRoughnessMapUv), x.push(b.specularMapUv), x.push(b.specularColorMapUv), x.push(b.specularIntensityMapUv), x.push(b.transmissionMapUv), x.push(b.thicknessMapUv), x.push(b.combine), x.push(b.fogExp2), x.push(b.sizeAttenuation), x.push(b.morphTargetsCount), x.push(b.morphAttributeCount), x.push(b.numSunLights), x.push(b.numDirLights), x.push(b.numPointLights), x.push(b.numSpotLights), x.push(b.numSpotLightMaps), x.push(b.numHemiLights), x.push(b.numRectAreaLights), x.push(b.numSunLightShadows), x.push(b.numDirLightShadows), x.push(b.numPointLightShadows), x.push(b.numSpotLightShadows), x.push(b.numSpotLightShadowsWithMaps), x.push(b.numLightProbes), x.push(b.shadowMapType), x.push(b.toneMapping), x.push(b.numClippingPlanes), x.push(b.numClipIntersection), x.push(b.depthPacking);
  }
  function E(x, b) {
    a.disableAll(), b.instancing && a.enable(0), b.instancingColor && a.enable(1), b.instancingMorph && a.enable(2), b.matcap && a.enable(3), b.envMap && a.enable(4), b.normalMapObjectSpace && a.enable(5), b.normalMapTangentSpace && a.enable(6), b.clearcoat && a.enable(7), b.iridescence && a.enable(8), b.alphaTest && a.enable(9), b.vertexColors && a.enable(10), b.vertexAlphas && a.enable(11), b.vertexUv1s && a.enable(12), b.vertexUv2s && a.enable(13), b.vertexUv3s && a.enable(14), b.vertexTangents && a.enable(15), b.anisotropy && a.enable(16), b.alphaHash && a.enable(17), b.batching && a.enable(18), b.dispersion && a.enable(19), b.retroreflection && a.enable(24), b.batchingColor && a.enable(20), b.gradientMap && a.enable(21), b.packedNormalMap && a.enable(22), b.vertexNormals && a.enable(23), x.push(a.mask), a.disableAll(), b.fog && a.enable(0), b.useFog && a.enable(1), b.flatShading && a.enable(2), b.logarithmicDepthBuffer && a.enable(3), b.reversedDepthBuffer && a.enable(4), b.skinning && a.enable(5), b.morphTargets && a.enable(6), b.morphNormals && a.enable(7), b.morphColors && a.enable(8), b.premultipliedAlpha && a.enable(9), b.shadowMapEnabled && a.enable(10), b.doubleSided && a.enable(11), b.flipSided && a.enable(12), b.useDepthPacking && a.enable(13), b.dithering && a.enable(14), b.transmission && a.enable(15), b.sheen && a.enable(16), b.opaque && a.enable(17), b.pointsUvs && a.enable(18), b.decodeVideoTexture && a.enable(19), b.decodeVideoTextureEmissive && a.enable(20), b.alphaToCoverage && a.enable(21), b.numLightProbeGrids > 0 && a.enable(22), b.hasPositionAttribute && a.enable(23), x.push(a.mask);
  }
  function R(x) {
    const b = p[x.type];
    let N;
    if (b) {
      const O = Pi[b];
      N = sd.clone(O.uniforms);
    } else N = x.uniforms;
    return N;
  }
  function y(x, b) {
    let N = h.get(b);
    return N !== void 0 ? ++N.usedTimes : (N = new Z0(r, b, x, n), c.push(N), h.set(b, N)), N;
  }
  function S(x) {
    if (--x.usedTimes === 0) {
      const b = c.indexOf(x);
      c[b] = c[c.length - 1], c.pop(), h.delete(x.cacheKey), x.destroy();
    }
  }
  function T(x) {
    o.remove(x);
  }
  function A() {
    o.dispose();
  }
  return { getParameters: v, getProgramCacheKey: m, getUniforms: R, acquireProgram: y, releaseProgram: S, releaseShaderCache: T, programs: c, dispose: A };
}
function ix() {
  let r = /* @__PURE__ */ new WeakMap();
  function e(a) {
    return r.has(a);
  }
  function t(a) {
    let o = r.get(a);
    return o === void 0 && (o = {}, r.set(a, o)), o;
  }
  function i(a) {
    r.delete(a);
  }
  function n(a, o, l) {
    r.get(a)[o] = l;
  }
  function s() {
    r = /* @__PURE__ */ new WeakMap();
  }
  return { has: e, get: t, remove: i, update: n, dispose: s };
}
function rx(r, e) {
  return r.groupOrder !== e.groupOrder ? r.groupOrder - e.groupOrder : r.renderOrder !== e.renderOrder ? r.renderOrder - e.renderOrder : r.material.id !== e.material.id ? r.material.id - e.material.id : r.materialVariant !== e.materialVariant ? r.materialVariant - e.materialVariant : r.z !== e.z ? r.z - e.z : r.id - e.id;
}
function vh(r, e) {
  return r.groupOrder !== e.groupOrder ? r.groupOrder - e.groupOrder : r.renderOrder !== e.renderOrder ? r.renderOrder - e.renderOrder : r.z !== e.z ? e.z - r.z : r.id - e.id;
}
function xh() {
  const r = [];
  let e = 0;
  const t = [], i = [], n = [];
  function s() {
    e = 0, t.length = 0, i.length = 0, n.length = 0;
  }
  function a(u) {
    let p = 0;
    return u.isInstancedMesh && (p += 2), u.isSkinnedMesh && (p += 1), p;
  }
  function o(u, p, f, v, m, g) {
    let E = r[e];
    return E === void 0 ? (E = { id: u.id, object: u, geometry: p, material: f, materialVariant: a(u), groupOrder: v, renderOrder: u.renderOrder, z: m, group: g }, r[e] = E) : (E.id = u.id, E.object = u, E.geometry = p, E.material = f, E.materialVariant = a(u), E.groupOrder = v, E.renderOrder = u.renderOrder, E.z = m, E.group = g), e++, E;
  }
  function l(u, p, f, v, m, g, E) {
    E.reversedDepth === !0 && (m = -m);
    const R = o(u, p, f, v, m, g);
    f.transmission > 0 ? i.push(R) : f.transparent === !0 ? n.push(R) : t.push(R);
  }
  function c(u, p, f, v, m, g) {
    const E = o(u, p, f, v, m, g);
    f.transmission > 0 ? i.unshift(E) : f.transparent === !0 ? n.unshift(E) : t.unshift(E);
  }
  function h(u, p) {
    t.length > 1 && t.sort(u || rx), i.length > 1 && i.sort(p || vh), n.length > 1 && n.sort(p || vh);
  }
  function d() {
    for (let u = e, p = r.length; u < p; u++) {
      const f = r[u];
      if (f.id === null) break;
      f.id = null, f.object = null, f.geometry = null, f.material = null, f.group = null;
    }
  }
  return { opaque: t, transmissive: i, transparent: n, init: s, push: l, unshift: c, finish: d, sort: h };
}
function nx() {
  let r = /* @__PURE__ */ new WeakMap();
  function e(i, n) {
    const s = r.get(i);
    let a;
    return s === void 0 ? (a = new xh(), r.set(i, [a])) : n >= s.length ? (a = new xh(), s.push(a)) : a = s[n], a;
  }
  function t() {
    r = /* @__PURE__ */ new WeakMap();
  }
  return { get: e, dispose: t };
}
function sx() {
  const r = {};
  return { get: function(e) {
    if (r[e.id] !== void 0) return r[e.id];
    let t;
    switch (e.type) {
      case "SunLight":
      case "DirectionalLight":
        t = { direction: new P(), color: new be() };
        break;
      case "SpotLight":
        t = { position: new P(), direction: new P(), color: new be(), distance: 0, coneCos: 0, penumbraCos: 0, decay: 0 };
        break;
      case "PointLight":
        t = { position: new P(), color: new be(), distance: 0, decay: 0 };
        break;
      case "HemisphereLight":
        t = { direction: new P(), skyColor: new be(), groundColor: new be() };
        break;
      case "RectAreaLight":
        t = { color: new be(), position: new P(), halfWidth: new P(), halfHeight: new P() };
        break;
    }
    return r[e.id] = t, t;
  } };
}
function ax() {
  const r = {};
  return { get: function(e) {
    if (r[e.id] !== void 0) return r[e.id];
    let t;
    switch (e.type) {
      case "SunLight":
      case "DirectionalLight":
        t = { shadowIntensity: 1, shadowBias: 0, shadowNormalBias: 0, shadowRadius: 1, shadowMapSize: new We() };
        break;
      case "SpotLight":
        t = { shadowIntensity: 1, shadowBias: 0, shadowNormalBias: 0, shadowRadius: 1, shadowMapSize: new We() };
        break;
      case "PointLight":
        t = { shadowIntensity: 1, shadowBias: 0, shadowNormalBias: 0, shadowRadius: 1, shadowMapSize: new We(), shadowCameraNear: 1, shadowCameraFar: 1e3 };
        break;
    }
    return r[e.id] = t, t;
  } };
}
let ox = 0;
function lx(r, e) {
  return (e.castShadow ? 2 : 0) - (r.castShadow ? 2 : 0) + (e.map ? 1 : 0) - (r.map ? 1 : 0);
}
function cx(r) {
  const e = new sx(), t = ax(), i = { version: 0, hash: { sunLength: -1, directionalLength: -1, pointLength: -1, spotLength: -1, rectAreaLength: -1, hemiLength: -1, numSunShadows: -1, numDirectionalShadows: -1, numPointShadows: -1, numSpotShadows: -1, numSpotMaps: -1, numLightProbes: -1 }, ambient: [0, 0, 0], probe: [], sun: [], sunShadow: [], sunShadowMap: [], sunShadowMatrix: [], sunShadowCascade: [], directional: [], directionalShadow: [], directionalShadowMap: [], directionalShadowMatrix: [], spot: [], spotLightMap: [], spotShadow: [], spotShadowMap: [], spotLightMatrix: [], rectArea: [], rectAreaLTC1: null, rectAreaLTC2: null, point: [], pointShadow: [], pointShadowMap: [], pointShadowMatrix: [], hemi: [], numSpotLightShadowsWithMaps: 0, numLightProbes: 0 };
  for (let c = 0; c < 9; c++) i.probe.push(new P());
  const n = new P(), s = new Fe(), a = new Fe();
  function o(c) {
    let h = 0, d = 0, u = 0;
    for (let k = 0; k < 9; k++) i.probe[k].set(0, 0, 0);
    let p = 0, f = 0, v = 0, m = 0, g = 0, E = 0, R = 0, y = 0, S = 0, T = 0, A = 0, x = 0, b = 0, N = 0;
    c.sort(lx);
    for (let k = 0, q = c.length; k < q; k++) {
      const B = c[k], G = B.color, Z = B.intensity, X = B.distance;
      let te = null;
      if (B.shadow && B.shadow.map && (B.shadow.map.texture.format === Ur ? te = B.shadow.map.texture : te = B.shadow.map.depthTexture || B.shadow.map.texture), B.isAmbientLight) h += G.r * Z, d += G.g * Z, u += G.b * Z;
      else if (B.isLightProbe) {
        for (let V = 0; V < 9; V++) i.probe[V].addScaledVector(B.sh.coefficients[V], Z);
        N++;
      } else if (B.isSunLight) {
        const V = e.get(B);
        if (V.color.copy(B.color).multiplyScalar(B.intensity), B.castShadow) {
          const K = B.shadow, ee = t.get(B);
          ee.shadowIntensity = K.intensity, ee.shadowBias = K.bias, ee.shadowNormalBias = K.normalBias, ee.shadowRadius = K.radius, ee.shadowMapSize.copy(K.mapSize).multiply(K.getFrameExtents()), i.sunShadow[f] = ee, i.sunShadowMap[f] = te;
          const Ue = K.getViewportCount();
          for (let ue = 0; ue < Ue; ue++) i.sunShadowMatrix[v + ue] = K.getMatrix(ue), i.sunShadowCascade[v + ue] = K._cascadeData[ue];
          v += Ue, f++;
        }
        i.sun[p] = V, p++;
      } else if (B.isDirectionalLight) {
        const V = e.get(B);
        if (V.color.copy(B.color).multiplyScalar(B.intensity), B.castShadow) {
          const K = B.shadow, ee = t.get(B);
          ee.shadowIntensity = K.intensity, ee.shadowBias = K.bias, ee.shadowNormalBias = K.normalBias, ee.shadowRadius = K.radius, ee.shadowMapSize = K.mapSize, i.directionalShadow[m] = ee, i.directionalShadowMap[m] = te, i.directionalShadowMatrix[m] = B.shadow.matrix, S++;
        }
        i.directional[m] = V, m++;
      } else if (B.isSpotLight) {
        const V = e.get(B);
        V.position.setFromMatrixPosition(B.matrixWorld), V.color.copy(G).multiplyScalar(Z), V.distance = X, V.coneCos = Math.cos(B.angle), V.penumbraCos = Math.cos(B.angle * (1 - B.penumbra)), V.decay = B.decay, i.spot[E] = V;
        const K = B.shadow;
        if (B.map && (i.spotLightMap[x] = B.map, x++, K.updateMatrices(B), B.castShadow && b++), i.spotLightMatrix[E] = K.matrix, B.castShadow) {
          const ee = t.get(B);
          ee.shadowIntensity = K.intensity, ee.shadowBias = K.bias, ee.shadowNormalBias = K.normalBias, ee.shadowRadius = K.radius, ee.shadowMapSize = K.mapSize, i.spotShadow[E] = ee, i.spotShadowMap[E] = te, A++;
        }
        E++;
      } else if (B.isRectAreaLight) {
        const V = e.get(B);
        V.color.copy(G).multiplyScalar(Z), V.halfWidth.set(B.width * 0.5, 0, 0), V.halfHeight.set(0, B.height * 0.5, 0), i.rectArea[R] = V, R++;
      } else if (B.isPointLight) {
        const V = e.get(B);
        if (V.color.copy(B.color).multiplyScalar(B.intensity), V.distance = B.distance, V.decay = B.decay, B.castShadow) {
          const K = B.shadow, ee = t.get(B);
          ee.shadowIntensity = K.intensity, ee.shadowBias = K.bias, ee.shadowNormalBias = K.normalBias, ee.shadowRadius = K.radius, ee.shadowMapSize = K.mapSize, ee.shadowCameraNear = K.camera.near, ee.shadowCameraFar = K.camera.far, i.pointShadow[g] = ee, i.pointShadowMap[g] = te, i.pointShadowMatrix[g] = B.shadow.matrix, T++;
        }
        i.point[g] = V, g++;
      } else if (B.isHemisphereLight) {
        const V = e.get(B);
        V.skyColor.copy(B.color).multiplyScalar(Z), V.groundColor.copy(B.groundColor).multiplyScalar(Z), i.hemi[y] = V, y++;
      }
    }
    R > 0 && (r.has("OES_texture_float_linear") === !0 ? (i.rectAreaLTC1 = fe.LTC_FLOAT_1, i.rectAreaLTC2 = fe.LTC_FLOAT_2) : (i.rectAreaLTC1 = fe.LTC_HALF_1, i.rectAreaLTC2 = fe.LTC_HALF_2)), i.ambient[0] = h, i.ambient[1] = d, i.ambient[2] = u;
    const O = i.hash;
    (O.sunLength !== p || O.directionalLength !== m || O.pointLength !== g || O.spotLength !== E || O.rectAreaLength !== R || O.hemiLength !== y || O.numSunShadows !== f || O.numDirectionalShadows !== S || O.numPointShadows !== T || O.numSpotShadows !== A || O.numSpotMaps !== x || O.numLightProbes !== N) && (i.sun.length = p, i.directional.length = m, i.spot.length = E, i.rectArea.length = R, i.point.length = g, i.hemi.length = y, i.sunShadow.length = f, i.sunShadowMap.length = f, i.sunShadowMatrix.length = v, i.sunShadowCascade.length = v, i.directionalShadow.length = S, i.directionalShadowMap.length = S, i.directionalShadowMatrix.length = S, i.pointShadow.length = T, i.pointShadowMap.length = T, i.pointShadowMatrix.length = T, i.spotShadow.length = A, i.spotShadowMap.length = A, i.spotLightMatrix.length = A + x - b, i.spotLightMap.length = x, i.numSpotLightShadowsWithMaps = b, i.numLightProbes = N, O.sunLength = p, O.directionalLength = m, O.pointLength = g, O.spotLength = E, O.rectAreaLength = R, O.hemiLength = y, O.numSunShadows = f, O.numDirectionalShadows = S, O.numPointShadows = T, O.numSpotShadows = A, O.numSpotMaps = x, O.numLightProbes = N, i.version = ox++);
  }
  function l(c, h) {
    let d = 0, u = 0, p = 0, f = 0, v = 0, m = 0;
    const g = h.matrixWorldInverse;
    for (let E = 0, R = c.length; E < R; E++) {
      const y = c[E];
      if (y.isSunLight) {
        const S = i.sun[d];
        S.direction.setFromMatrixPosition(y.matrixWorld), S.direction.transformDirection(g), d++;
      } else if (y.isDirectionalLight) {
        const S = i.directional[u];
        S.direction.setFromMatrixPosition(y.matrixWorld), n.setFromMatrixPosition(y.target.matrixWorld), S.direction.sub(n), S.direction.transformDirection(g), u++;
      } else if (y.isSpotLight) {
        const S = i.spot[f];
        S.position.setFromMatrixPosition(y.matrixWorld), S.position.applyMatrix4(g), S.direction.setFromMatrixPosition(y.matrixWorld), n.setFromMatrixPosition(y.target.matrixWorld), S.direction.sub(n), S.direction.transformDirection(g), f++;
      } else if (y.isRectAreaLight) {
        const S = i.rectArea[v];
        S.position.setFromMatrixPosition(y.matrixWorld), S.position.applyMatrix4(g), a.identity(), s.copy(y.matrixWorld), s.premultiply(g), a.extractRotation(s), S.halfWidth.set(y.width * 0.5, 0, 0), S.halfHeight.set(0, y.height * 0.5, 0), S.halfWidth.applyMatrix4(a), S.halfHeight.applyMatrix4(a), v++;
      } else if (y.isPointLight) {
        const S = i.point[p];
        S.position.setFromMatrixPosition(y.matrixWorld), S.position.applyMatrix4(g), p++;
      } else if (y.isHemisphereLight) {
        const S = i.hemi[m];
        S.direction.setFromMatrixPosition(y.matrixWorld), S.direction.transformDirection(g), m++;
      }
    }
  }
  return { setup: o, setupView: l, state: i };
}
function Mh(r) {
  const e = new cx(r), t = [], i = [], n = [];
  function s(u) {
    d.camera = u, t.length = 0, i.length = 0, n.length = 0;
  }
  function a(u) {
    t.push(u);
  }
  function o(u) {
    i.push(u);
  }
  function l(u) {
    n.push(u);
  }
  function c() {
    e.setup(t);
  }
  function h(u) {
    e.setupView(t, u);
  }
  const d = { lightsArray: t, shadowsArray: i, lightProbeGridArray: n, camera: null, lights: e, transmissionRenderTarget: {}, textureUnits: 0 };
  return { init: s, state: d, setupLights: c, setupLightsView: h, pushLight: a, pushShadow: o, pushLightProbeGrid: l };
}
function hx(r) {
  let e = /* @__PURE__ */ new WeakMap();
  function t(n, s = 0) {
    const a = e.get(n);
    let o;
    return a === void 0 ? (o = new Mh(r), e.set(n, [o])) : s >= a.length ? (o = new Mh(r), a.push(o)) : o = a[s], o;
  }
  function i() {
    e = /* @__PURE__ */ new WeakMap();
  }
  return { get: t, dispose: i };
}
const ux = `void main() {
	gl_Position = vec4( position, 1.0 );
}`, dx = `uniform sampler2D shadow_pass;
uniform vec2 resolution;
uniform float radius;
void main() {
	const float samples = float( VSM_SAMPLES );
	float mean = 0.0;
	float squared_mean = 0.0;
	float uvStride = samples <= 1.0 ? 0.0 : 2.0 / ( samples - 1.0 );
	float uvStart = samples <= 1.0 ? 0.0 : - 1.0;
	for ( float i = 0.0; i < samples; i ++ ) {
		float uvOffset = uvStart + i * uvStride;
		#ifdef HORIZONTAL_PASS
			vec2 distribution = texture2D( shadow_pass, ( gl_FragCoord.xy + vec2( uvOffset, 0.0 ) * radius ) / resolution ).rg;
			mean += distribution.x;
			squared_mean += distribution.y * distribution.y + distribution.x * distribution.x;
		#else
			float depth = texture2D( shadow_pass, ( gl_FragCoord.xy + vec2( 0.0, uvOffset ) * radius ) / resolution ).r;
			mean += depth;
			squared_mean += depth * depth;
		#endif
	}
	mean = mean / samples;
	squared_mean = squared_mean / samples;
	float std_dev = sqrt( max( 0.0, squared_mean - mean * mean ) );
	gl_FragColor = vec4( mean, std_dev, 0.0, 1.0 );
}`, px = [new P(1, 0, 0), new P(-1, 0, 0), new P(0, 1, 0), new P(0, -1, 0), new P(0, 0, 1), new P(0, 0, -1)], fx = [new P(0, -1, 0), new P(0, -1, 0), new P(0, 0, 1), new P(0, 0, -1), new P(0, -1, 0), new P(0, -1, 0)], yh = new Fe(), Bn = new P(), uo = new P();
function mx(r, e, t) {
  let i = new Kl();
  const n = new We(), s = new We(), a = new gt(), o = new jf(), l = new qf(), c = {}, h = t.maxTextureSize, d = { [fr]: Gt, [Gt]: fr, [ai]: ai }, u = new xi({ defines: { VSM_SAMPLES: 8 }, uniforms: { shadow_pass: { value: null }, resolution: { value: new We() }, radius: { value: 4 } }, vertexShader: ux, fragmentShader: dx }), p = u.clone();
  p.defines.HORIZONTAL_PASS = 1;
  const f = new yt();
  f.setAttribute("position", new nt(new Float32Array([-1, -1, 0.5, 3, -1, 0.5, -1, 3, 0.5]), 3));
  const v = new Wt(f, u), m = this;
  this.enabled = !1, this.autoUpdate = !0, this.needsUpdate = !1, this.type = qs;
  let g = this.type;
  this.render = function(T, A, x) {
    if (m.enabled === !1 || m.autoUpdate === !1 && m.needsUpdate === !1 || T.length === 0) return;
    this.type === hp && (Re("WebGLShadowMap: PCFSoftShadowMap has been removed. Using PCFShadowMap instead."), this.type = qs);
    const b = r.getRenderTarget(), N = r.getActiveCubeFace(), O = r.getActiveMipmapLevel(), k = r.state;
    k.setBlending($i), k.buffers.depth.getReversed() === !0 ? k.buffers.color.setClear(0, 0, 0, 0) : k.buffers.color.setClear(1, 1, 1, 1), k.buffers.depth.setTest(!0), k.setScissorTest(!1);
    const q = g !== this.type;
    q && A.traverse(function(B) {
      B.material && (Array.isArray(B.material) ? B.material.forEach((G) => G.needsUpdate = !0) : B.material.needsUpdate = !0);
    });
    for (let B = 0, G = T.length; B < G; B++) {
      const Z = T[B], X = Z.shadow;
      if (X === void 0) {
        Re("WebGLShadowMap:", Z, "has no shadow.");
        continue;
      }
      if (X.autoUpdate === !1 && X.needsUpdate === !1) continue;
      n.copy(X.mapSize);
      const te = X.getFrameExtents();
      n.multiply(te), s.copy(X.mapSize), (n.x > h || n.y > h) && (n.x > h && (s.x = Math.floor(h / te.x), n.x = s.x * te.x, X.mapSize.x = s.x), n.y > h && (s.y = Math.floor(h / te.y), n.y = s.y * te.y, X.mapSize.y = s.y));
      const V = r.state.buffers.depth.getReversed();
      if (X.camera._reversedDepth = V, X.map === null || q === !0) {
        if (X.map !== null && (X.map.depthTexture !== null && (X.map.depthTexture.dispose(), X.map.depthTexture = null), X.map.dispose()), this.type === qn) {
          if (Z.isPointLight) {
            Re("WebGLShadowMap: VSM shadow maps are not supported for PointLights. Use PCF or BasicShadowMap instead.");
            continue;
          }
          X.map = new _i(n.x, n.y, { format: Ur, type: Ui, minFilter: Pt, magFilter: Pt, generateMipmaps: !1 }), X.map.texture.name = Z.name + ".shadowMap", X.map.depthTexture = new cs(n.x, n.y, oi), X.map.depthTexture.name = Z.name + ".shadowMapDepth", X.map.depthTexture.format = Ji, X.map.depthTexture.compareFunction = null, X.map.depthTexture.minFilter = Rt, X.map.depthTexture.magFilter = Rt;
        } else Z.isPointLight ? (X.map = new md(n.x), X.map.depthTexture = new kf(n.x, Ni)) : (X.map = new _i(n.x, n.y), X.map.depthTexture = new cs(n.x, n.y, Ni)), X.map.depthTexture.name = Z.name + ".shadowMap", X.map.depthTexture.format = Ji, this.type === qs ? (X.map.depthTexture.compareFunction = V ? Wl : Gl, X.map.depthTexture.minFilter = Pt, X.map.depthTexture.magFilter = Pt) : (X.map.depthTexture.compareFunction = null, X.map.depthTexture.minFilter = Rt, X.map.depthTexture.magFilter = Rt);
        X.camera.updateProjectionMatrix();
      }
      X.map.isWebGLCubeRenderTarget !== !0 && (X.map.width !== n.x || X.map.height !== n.y) && X.map.setSize(n.x, n.y);
      const K = X.map.isWebGLCubeRenderTarget ? 6 : X.getViewportCount();
      Z.isPointLight !== !0 && X.updateMatrices(Z, x);
      for (let ee = 0; ee < K; ee++) {
        const Ue = X.getCamera(ee);
        if (Z.isPointLight) {
          const ue = X.camera, Ye = X.matrix, ke = Z.distance || ue.far;
          ke !== ue.far && (ue.far = ke, ue.updateProjectionMatrix()), Bn.setFromMatrixPosition(Z.matrixWorld), ue.position.copy(Bn), uo.copy(ue.position), uo.add(px[ee]), ue.up.copy(fx[ee]), ue.lookAt(uo), ue.updateMatrixWorld(), Ye.makeTranslation(-Bn.x, -Bn.y, -Bn.z), yh.multiplyMatrices(ue.projectionMatrix, ue.matrixWorldInverse), X._frustum.setFromProjectionMatrix(yh, ue.coordinateSystem, ue.reversedDepth);
        }
        if (X.map.isWebGLCubeRenderTarget) r.setRenderTarget(X.map, ee), r.clear();
        else {
          ee === 0 && (r.setRenderTarget(X.map), r.clear());
          const ue = X.getViewport(ee);
          a.set(s.x * ue.x, s.y * ue.y, s.x * ue.z, s.y * ue.w), k.viewport(a);
        }
        i = X.getFrustum(ee), y(A, x, Ue, Z, this.type);
      }
      X.isPointLightShadow !== !0 && this.type === qn && E(X, x), X.needsUpdate = !1;
    }
    g = this.type, m.needsUpdate = !1, r.setRenderTarget(b, N, O);
  };
  function E(T, A) {
    const x = e.update(v);
    u.defines.VSM_SAMPLES !== T.blurSamples && (u.defines.VSM_SAMPLES = T.blurSamples, p.defines.VSM_SAMPLES = T.blurSamples, u.needsUpdate = !0, p.needsUpdate = !0), T.mapPass === null ? T.mapPass = new _i(n.x, n.y, { format: Ur, type: Ui }) : (T.mapPass.width !== T.map.width || T.mapPass.height !== T.map.height) && T.mapPass.setSize(T.map.width, T.map.height), u.uniforms.shadow_pass.value = T.map.depthTexture, u.uniforms.resolution.value.set(T.map.width, T.map.height), u.uniforms.radius.value = T.radius, r.setRenderTarget(T.mapPass), r.clear(), r.renderBufferDirect(A, null, x, u, v, null), p.uniforms.shadow_pass.value = T.mapPass.texture, p.uniforms.resolution.value.set(T.map.width, T.map.height), p.uniforms.radius.value = T.radius, r.setRenderTarget(T.map), r.clear(), r.renderBufferDirect(A, null, x, p, v, null);
  }
  function R(T, A, x, b) {
    let N = null;
    const O = x.isPointLight === !0 ? T.customDistanceMaterial : T.customDepthMaterial;
    if (O !== void 0) N = O;
    else if (N = x.isPointLight === !0 ? l : o, r.localClippingEnabled && A.clipShadows === !0 && Array.isArray(A.clippingPlanes) && A.clippingPlanes.length !== 0 || A.displacementMap && A.displacementScale !== 0 || A.alphaMap && A.alphaTest > 0 || A.map && A.alphaTest > 0 || A.alphaToCoverage === !0) {
      const k = N.uuid, q = A.uuid;
      let B = c[k];
      B === void 0 && (B = {}, c[k] = B);
      let G = B[q];
      G === void 0 && (G = N.clone(), B[q] = G, A.addEventListener("dispose", S)), N = G;
    }
    if (N.visible = A.visible, N.wireframe = A.wireframe, b === qn ? N.side = A.shadowSide !== null ? A.shadowSide : A.side : N.side = A.shadowSide !== null ? A.shadowSide : d[A.side], N.alphaMap = A.alphaMap, N.alphaTest = A.alphaToCoverage === !0 ? 0.5 : A.alphaTest, N.map = A.map, N.clipShadows = A.clipShadows, N.clippingPlanes = A.clippingPlanes, N.clipIntersection = A.clipIntersection, N.displacementMap = A.displacementMap, N.displacementScale = A.displacementScale, N.displacementBias = A.displacementBias, N.wireframeLinewidth = A.wireframeLinewidth, N.linewidth = A.linewidth, x.isPointLight === !0 && N.isMeshDistanceMaterial === !0) {
      const k = r.properties.get(N);
      k.light = x;
    }
    return N;
  }
  function y(T, A, x, b, N) {
    if (T.visible === !1) return;
    if (T.layers.test(A.layers) && (T.isMesh || T.isLine || T.isPoints) && (T.castShadow || T.receiveShadow && N === qn) && (!T.frustumCulled || T.intersectsFrustum(i))) {
      T.modelViewMatrix.multiplyMatrices(x.matrixWorldInverse, T.matrixWorld);
      const k = e.update(T), q = T.material;
      if (Array.isArray(q)) {
        const B = k.groups;
        for (let G = 0, Z = B.length; G < Z; G++) {
          const X = B[G], te = q[X.materialIndex];
          if (te && te.visible) {
            const V = R(T, te, b, N);
            T.onBeforeShadow(r, T, A, x, k, V, X), r.renderBufferDirect(x, null, k, V, T, X), T.onAfterShadow(r, T, A, x, k, V, X);
          }
        }
      } else if (q.visible) {
        const B = R(T, q, b, N);
        T.onBeforeShadow(r, T, A, x, k, B, null), r.renderBufferDirect(x, null, k, B, T, null), T.onAfterShadow(r, T, A, x, k, B, null);
      }
    }
    const O = T.children;
    for (let k = 0, q = O.length; k < q; k++) y(O[k], A, x, b, N);
  }
  function S(T) {
    T.target.removeEventListener("dispose", S);
    for (const A in c) {
      const x = c[A], b = T.target.uuid;
      b in x && (x[b].dispose(), delete x[b]);
    }
  }
}
function gx(r, e) {
  function t() {
    let U = !1;
    const Q = new gt();
    let re = null;
    const _e = new gt(0, 0, 0, 0);
    return { setMask: function(ae) {
      re !== ae && !U && (r.colorMask(ae, ae, ae, ae), re = ae);
    }, setLocked: function(ae) {
      U = ae;
    }, setClear: function(ae, J, me, pe, Le) {
      Le === !0 && (ae *= pe, J *= pe, me *= pe), Q.set(ae, J, me, pe), _e.equals(Q) === !1 && (r.clearColor(ae, J, me, pe), _e.copy(Q));
    }, reset: function() {
      U = !1, re = null, _e.set(-1, 0, 0, 0);
    } };
  }
  function i() {
    let U = !1, Q = !1, re = null, _e = null, ae = null;
    return { setReversed: function(J) {
      if (Q !== J) {
        const me = e.get("EXT_clip_control");
        J ? me.clipControlEXT(me.LOWER_LEFT_EXT, me.ZERO_TO_ONE_EXT) : me.clipControlEXT(me.LOWER_LEFT_EXT, me.NEGATIVE_ONE_TO_ONE_EXT), Q = J;
        const pe = ae;
        ae = null, this.setClear(pe);
      }
    }, getReversed: function() {
      return Q;
    }, setTest: function(J) {
      J ? $(r.DEPTH_TEST) : le(r.DEPTH_TEST);
    }, setMask: function(J) {
      re !== J && !U && (r.depthMask(J), re = J);
    }, setFunc: function(J) {
      if (Q && (J = Kp[J]), _e !== J) {
        switch (J) {
          case Oo:
            r.depthFunc(r.NEVER);
            break;
          case Fo:
            r.depthFunc(r.ALWAYS);
            break;
          case Bo:
            r.depthFunc(r.LESS);
            break;
          case is:
            r.depthFunc(r.LEQUAL);
            break;
          case Vo:
            r.depthFunc(r.EQUAL);
            break;
          case Ho:
            r.depthFunc(r.GEQUAL);
            break;
          case ko:
            r.depthFunc(r.GREATER);
            break;
          case zo:
            r.depthFunc(r.NOTEQUAL);
            break;
          default:
            r.depthFunc(r.LEQUAL);
        }
        _e = J;
      }
    }, setLocked: function(J) {
      U = J;
    }, setClear: function(J) {
      ae !== J && (ae = J, Q && (J = 1 - J), r.clearDepth(J));
    }, reset: function() {
      U = !1, re = null, _e = null, ae = null, Q = !1;
    } };
  }
  function n() {
    let U = !1, Q = null, re = null, _e = null, ae = null, J = null, me = null, pe = null, Le = null;
    return { setTest: function(Ne) {
      U || (Ne ? $(r.STENCIL_TEST) : le(r.STENCIL_TEST));
    }, setMask: function(Ne) {
      Q !== Ne && !U && (r.stencilMask(Ne), Q = Ne);
    }, setFunc: function(Ne, Ut, Xt) {
      (re !== Ne || _e !== Ut || ae !== Xt) && (r.stencilFunc(Ne, Ut, Xt), re = Ne, _e = Ut, ae = Xt);
    }, setOp: function(Ne, Ut, Xt) {
      (J !== Ne || me !== Ut || pe !== Xt) && (r.stencilOp(Ne, Ut, Xt), J = Ne, me = Ut, pe = Xt);
    }, setLocked: function(Ne) {
      U = Ne;
    }, setClear: function(Ne) {
      Le !== Ne && (r.clearStencil(Ne), Le = Ne);
    }, reset: function() {
      U = !1, Q = null, re = null, _e = null, ae = null, J = null, me = null, pe = null, Le = null;
    } };
  }
  const s = new t(), a = new i(), o = new n(), l = /* @__PURE__ */ new WeakMap(), c = /* @__PURE__ */ new WeakMap();
  let h = {}, d = {}, u = {}, p = /* @__PURE__ */ new WeakMap(), f = [], v = null, m = !1, g = null, E = null, R = null, y = null, S = null, T = null, A = null, x = new be(0, 0, 0), b = 0, N = !1, O = null, k = null, q = null, B = null, G = null;
  const Z = r.getParameter(r.MAX_COMBINED_TEXTURE_IMAGE_UNITS);
  let X = !1, te = 0;
  const V = r.getParameter(r.VERSION);
  V.indexOf("WebGL") !== -1 ? (te = parseFloat(/^WebGL (\d)/.exec(V)[1]), X = te >= 1) : V.indexOf("OpenGL ES") !== -1 && (te = parseFloat(/^OpenGL ES (\d)/.exec(V)[1]), X = te >= 2);
  let K = null, ee = {};
  const Ue = r.getParameter(r.SCISSOR_BOX), ue = r.getParameter(r.VIEWPORT), Ye = new gt().fromArray(Ue), ke = new gt().fromArray(ue);
  function je(U, Q, re, _e) {
    const ae = new Uint8Array(4), J = r.createTexture();
    r.bindTexture(U, J), r.texParameteri(U, r.TEXTURE_MIN_FILTER, r.NEAREST), r.texParameteri(U, r.TEXTURE_MAG_FILTER, r.NEAREST);
    for (let me = 0; me < re; me++) U === r.TEXTURE_3D || U === r.TEXTURE_2D_ARRAY ? r.texImage3D(Q, 0, r.RGBA, 1, 1, _e, 0, r.RGBA, r.UNSIGNED_BYTE, ae) : r.texImage2D(Q + me, 0, r.RGBA, 1, 1, 0, r.RGBA, r.UNSIGNED_BYTE, ae);
    return J;
  }
  const Y = {};
  Y[r.TEXTURE_2D] = je(r.TEXTURE_2D, r.TEXTURE_2D, 1), Y[r.TEXTURE_CUBE_MAP] = je(r.TEXTURE_CUBE_MAP, r.TEXTURE_CUBE_MAP_POSITIVE_X, 6), Y[r.TEXTURE_2D_ARRAY] = je(r.TEXTURE_2D_ARRAY, r.TEXTURE_2D_ARRAY, 1, 1), Y[r.TEXTURE_3D] = je(r.TEXTURE_3D, r.TEXTURE_3D, 1, 1), s.setClear(0, 0, 0, 1), a.setClear(1), o.setClear(0), $(r.DEPTH_TEST), a.setFunc(is), Mt(!1), dt(lc), $(r.CULL_FACE), Ve($i);
  function $(U) {
    h[U] !== !0 && (r.enable(U), h[U] = !0);
  }
  function le(U) {
    h[U] !== !1 && (r.disable(U), h[U] = !1);
  }
  function Oe(U, Q) {
    return u[U] !== Q ? (r.bindFramebuffer(U, Q), u[U] = Q, U === r.DRAW_FRAMEBUFFER && (u[r.FRAMEBUFFER] = Q), U === r.FRAMEBUFFER && (u[r.DRAW_FRAMEBUFFER] = Q), !0) : !1;
  }
  function xe(U, Q) {
    let re = f, _e = !1;
    if (U) {
      re = p.get(Q), re === void 0 && (re = [], p.set(Q, re));
      const ae = U.textures;
      if (re.length !== ae.length || re[0] !== r.COLOR_ATTACHMENT0) {
        for (let J = 0, me = ae.length; J < me; J++) re[J] = r.COLOR_ATTACHMENT0 + J;
        re.length = ae.length, _e = !0;
      }
    } else re[0] !== r.BACK && (re[0] = r.BACK, _e = !0);
    _e && r.drawBuffers(re);
  }
  function Te(U) {
    return v !== U ? (r.useProgram(U), v = U, !0) : !1;
  }
  const He = { [nn]: r.FUNC_ADD, [dp]: r.FUNC_SUBTRACT, [pp]: r.FUNC_REVERSE_SUBTRACT };
  He[fp] = r.MIN, He[mp] = r.MAX;
  const Ke = { [gp]: r.ZERO, [_p]: r.ONE, [vp]: r.SRC_COLOR, [yu]: r.SRC_ALPHA, [Ep]: r.SRC_ALPHA_SATURATE, [Sp]: r.DST_COLOR, [Mp]: r.DST_ALPHA, [xp]: r.ONE_MINUS_SRC_COLOR, [Su]: r.ONE_MINUS_SRC_ALPHA, [Tp]: r.ONE_MINUS_DST_COLOR, [yp]: r.ONE_MINUS_DST_ALPHA, [bp]: r.CONSTANT_COLOR, [wp]: r.ONE_MINUS_CONSTANT_COLOR, [Ap]: r.CONSTANT_ALPHA, [Rp]: r.ONE_MINUS_CONSTANT_ALPHA };
  function Ve(U, Q, re, _e, ae, J, me, pe, Le, Ne) {
    if (U === $i) {
      m === !0 && (le(r.BLEND), m = !1);
      return;
    }
    if (m === !1 && ($(r.BLEND), m = !0), U !== up) {
      if (U !== g || Ne !== N) {
        if ((E !== nn || S !== nn) && (r.blendEquation(r.FUNC_ADD), E = nn, S = nn), Ne) switch (U) {
          case Qn:
            r.blendFuncSeparate(r.ONE, r.ONE_MINUS_SRC_ALPHA, r.ONE, r.ONE_MINUS_SRC_ALPHA);
            break;
          case cc:
            r.blendFunc(r.ONE, r.ONE);
            break;
          case hc:
            r.blendFuncSeparate(r.ZERO, r.ONE_MINUS_SRC_COLOR, r.ZERO, r.ONE);
            break;
          case uc:
            r.blendFuncSeparate(r.DST_COLOR, r.ONE_MINUS_SRC_ALPHA, r.ZERO, r.ONE);
            break;
          default:
            Be("WebGLState: Invalid blending: ", U);
            break;
        }
        else switch (U) {
          case Qn:
            r.blendFuncSeparate(r.SRC_ALPHA, r.ONE_MINUS_SRC_ALPHA, r.ONE, r.ONE_MINUS_SRC_ALPHA);
            break;
          case cc:
            r.blendFuncSeparate(r.SRC_ALPHA, r.ONE, r.ONE, r.ONE);
            break;
          case hc:
            Be("WebGLState: SubtractiveBlending requires material.premultipliedAlpha = true");
            break;
          case uc:
            Be("WebGLState: MultiplyBlending requires material.premultipliedAlpha = true");
            break;
          default:
            Be("WebGLState: Invalid blending: ", U);
            break;
        }
        R = null, y = null, T = null, A = null, x.set(0, 0, 0), b = 0, g = U, N = Ne;
      }
      return;
    }
    ae = ae || Q, J = J || re, me = me || _e, (Q !== E || ae !== S) && (r.blendEquationSeparate(He[Q], He[ae]), E = Q, S = ae), (re !== R || _e !== y || J !== T || me !== A) && (r.blendFuncSeparate(Ke[re], Ke[_e], Ke[J], Ke[me]), R = re, y = _e, T = J, A = me), (pe.equals(x) === !1 || Le !== b) && (r.blendColor(pe.r, pe.g, pe.b, Le), x.copy(pe), b = Le), g = U, N = !1;
  }
  function vt(U, Q) {
    U.side === ai ? le(r.CULL_FACE) : $(r.CULL_FACE);
    let re = U.side === Gt;
    Q && (re = !re), Mt(re), U.blending === Qn && U.transparent === !1 ? Ve($i) : Ve(U.blending, U.blendEquation, U.blendSrc, U.blendDst, U.blendEquationAlpha, U.blendSrcAlpha, U.blendDstAlpha, U.blendColor, U.blendAlpha, U.premultipliedAlpha), a.setFunc(U.depthFunc), a.setTest(U.depthTest), a.setMask(U.depthWrite), s.setMask(U.colorWrite);
    const _e = U.stencilWrite;
    o.setTest(_e), _e && (o.setMask(U.stencilWriteMask), o.setFunc(U.stencilFunc, U.stencilRef, U.stencilFuncMask), o.setOp(U.stencilFail, U.stencilZFail, U.stencilZPass)), At(U.polygonOffset, U.polygonOffsetFactor, U.polygonOffsetUnits), U.alphaToCoverage === !0 ? $(r.SAMPLE_ALPHA_TO_COVERAGE) : le(r.SAMPLE_ALPHA_TO_COVERAGE);
  }
  function Mt(U) {
    O !== U && (U ? r.frontFace(r.CW) : r.frontFace(r.CCW), O = U);
  }
  function dt(U) {
    U !== lp ? ($(r.CULL_FACE), U !== k && (U === lc ? r.cullFace(r.BACK) : U === cp ? r.cullFace(r.FRONT) : r.cullFace(r.FRONT_AND_BACK))) : le(r.CULL_FACE), k = U;
  }
  function Je(U) {
    U !== q && (X && r.lineWidth(U), q = U);
  }
  function At(U, Q, re) {
    U ? ($(r.POLYGON_OFFSET_FILL), (B !== Q || G !== re) && (B = Q, G = re, a.getReversed() && (Q = -Q), r.polygonOffset(Q, re))) : le(r.POLYGON_OFFSET_FILL);
  }
  function tt(U) {
    U ? $(r.SCISSOR_TEST) : le(r.SCISSOR_TEST);
  }
  function _t(U) {
    U === void 0 && (U = r.TEXTURE0 + Z - 1), K !== U && (r.activeTexture(U), K = U);
  }
  function C(U, Q, re) {
    re === void 0 && (K === null ? re = r.TEXTURE0 + Z - 1 : re = K);
    let _e = ee[re];
    _e === void 0 && (_e = { type: void 0, texture: void 0 }, ee[re] = _e), (_e.type !== U || _e.texture !== Q) && (K !== re && (r.activeTexture(re), K = re), r.bindTexture(U, Q || Y[U]), _e.type = U, _e.texture = Q);
  }
  function xt() {
    const U = ee[K];
    U !== void 0 && U.type !== void 0 && (r.bindTexture(U.type, null), U.type = void 0, U.texture = void 0);
  }
  function Qe() {
    try {
      r.compressedTexImage2D(...arguments);
    } catch (U) {
      Be("WebGLState:", U);
    }
  }
  function w() {
    try {
      r.compressedTexImage3D(...arguments);
    } catch (U) {
      Be("WebGLState:", U);
    }
  }
  function _() {
    try {
      r.texSubImage2D(...arguments);
    } catch (U) {
      Be("WebGLState:", U);
    }
  }
  function F() {
    try {
      r.texSubImage3D(...arguments);
    } catch (U) {
      Be("WebGLState:", U);
    }
  }
  function W() {
    try {
      r.compressedTexSubImage2D(...arguments);
    } catch (U) {
      Be("WebGLState:", U);
    }
  }
  function j() {
    try {
      r.compressedTexSubImage3D(...arguments);
    } catch (U) {
      Be("WebGLState:", U);
    }
  }
  function oe() {
    try {
      r.texStorage2D(...arguments);
    } catch (U) {
      Be("WebGLState:", U);
    }
  }
  function he() {
    try {
      r.texStorage3D(...arguments);
    } catch (U) {
      Be("WebGLState:", U);
    }
  }
  function D() {
    try {
      r.texImage2D(...arguments);
    } catch (U) {
      Be("WebGLState:", U);
    }
  }
  function se() {
    try {
      r.texImage3D(...arguments);
    } catch (U) {
      Be("WebGLState:", U);
    }
  }
  function ce(U) {
    return d[U] !== void 0 ? d[U] : r.getParameter(U);
  }
  function Me(U, Q) {
    d[U] !== Q && (r.pixelStorei(U, Q), d[U] = Q);
  }
  function ie(U) {
    Ye.equals(U) === !1 && (r.scissor(U.x, U.y, U.z, U.w), Ye.copy(U));
  }
  function ge(U) {
    ke.equals(U) === !1 && (r.viewport(U.x, U.y, U.z, U.w), ke.copy(U));
  }
  function Se(U, Q) {
    let re = c.get(Q);
    re === void 0 && (re = /* @__PURE__ */ new WeakMap(), c.set(Q, re));
    let _e = re.get(U);
    _e === void 0 && (_e = r.getUniformBlockIndex(Q, U.name), re.set(U, _e));
  }
  function Pe(U, Q) {
    const re = c.get(Q).get(U);
    l.get(Q) !== re && (r.uniformBlockBinding(Q, re, U.__bindingPointIndex), l.set(Q, re));
  }
  function Ie() {
    r.disable(r.BLEND), r.disable(r.CULL_FACE), r.disable(r.DEPTH_TEST), r.disable(r.POLYGON_OFFSET_FILL), r.disable(r.SCISSOR_TEST), r.disable(r.STENCIL_TEST), r.disable(r.SAMPLE_ALPHA_TO_COVERAGE), r.blendEquation(r.FUNC_ADD), r.blendFunc(r.ONE, r.ZERO), r.blendFuncSeparate(r.ONE, r.ZERO, r.ONE, r.ZERO), r.blendColor(0, 0, 0, 0), r.colorMask(!0, !0, !0, !0), r.clearColor(0, 0, 0, 0), r.depthMask(!0), r.depthFunc(r.LESS), a.setReversed(!1), r.clearDepth(1), r.stencilMask(4294967295), r.stencilFunc(r.ALWAYS, 0, 4294967295), r.stencilOp(r.KEEP, r.KEEP, r.KEEP), r.clearStencil(0), r.cullFace(r.BACK), r.frontFace(r.CCW), r.polygonOffset(0, 0), r.activeTexture(r.TEXTURE0), r.bindFramebuffer(r.FRAMEBUFFER, null), r.bindFramebuffer(r.DRAW_FRAMEBUFFER, null), r.bindFramebuffer(r.READ_FRAMEBUFFER, null), r.useProgram(null), r.lineWidth(1), r.scissor(0, 0, r.canvas.width, r.canvas.height), r.viewport(0, 0, r.canvas.width, r.canvas.height), r.pixelStorei(r.PACK_ALIGNMENT, 4), r.pixelStorei(r.UNPACK_ALIGNMENT, 4), r.pixelStorei(r.UNPACK_FLIP_Y_WEBGL, !1), r.pixelStorei(r.UNPACK_PREMULTIPLY_ALPHA_WEBGL, !1), r.pixelStorei(r.UNPACK_COLORSPACE_CONVERSION_WEBGL, r.BROWSER_DEFAULT_WEBGL), r.pixelStorei(r.PACK_ROW_LENGTH, 0), r.pixelStorei(r.PACK_SKIP_PIXELS, 0), r.pixelStorei(r.PACK_SKIP_ROWS, 0), r.pixelStorei(r.UNPACK_ROW_LENGTH, 0), r.pixelStorei(r.UNPACK_IMAGE_HEIGHT, 0), r.pixelStorei(r.UNPACK_SKIP_PIXELS, 0), r.pixelStorei(r.UNPACK_SKIP_ROWS, 0), r.pixelStorei(r.UNPACK_SKIP_IMAGES, 0), h = {}, d = {}, K = null, ee = {}, u = {}, p = /* @__PURE__ */ new WeakMap(), f = [], v = null, m = !1, g = null, E = null, R = null, y = null, S = null, T = null, A = null, x = new be(0, 0, 0), b = 0, N = !1, O = null, k = null, q = null, B = null, G = null, Ye.set(0, 0, r.canvas.width, r.canvas.height), ke.set(0, 0, r.canvas.width, r.canvas.height), s.reset(), a.reset(), o.reset();
  }
  return { buffers: { color: s, depth: a, stencil: o }, enable: $, disable: le, bindFramebuffer: Oe, drawBuffers: xe, useProgram: Te, setBlending: Ve, setMaterial: vt, setFlipSided: Mt, setCullFace: dt, setLineWidth: Je, setPolygonOffset: At, setScissorTest: tt, activeTexture: _t, bindTexture: C, unbindTexture: xt, compressedTexImage2D: Qe, compressedTexImage3D: w, texImage2D: D, texImage3D: se, pixelStorei: Me, getParameter: ce, updateUBOMapping: Se, uniformBlockBinding: Pe, texStorage2D: oe, texStorage3D: he, texSubImage2D: _, texSubImage3D: F, compressedTexSubImage2D: W, compressedTexSubImage3D: j, scissor: ie, viewport: ge, reset: Ie };
}
function _x(r, e, t, i, n, s, a) {
  const o = e.has("WEBGL_multisampled_render_to_texture") ? e.get("WEBGL_multisampled_render_to_texture") : null, l = typeof navigator > "u" ? !1 : /OculusBrowser/g.test(navigator.userAgent), c = new We(), h = /* @__PURE__ */ new WeakMap(), d = /* @__PURE__ */ new Set();
  let u;
  const p = /* @__PURE__ */ new WeakMap();
  let f = !1;
  try {
    f = typeof OffscreenCanvas < "u" && new OffscreenCanvas(1, 1).getContext("2d") !== null;
  } catch {
  }
  function v(w, _) {
    return f ? new OffscreenCanvas(w, _) : ls("canvas");
  }
  function m(w, _, F) {
    let W = 1;
    const j = Qe(w);
    if ((j.width > F || j.height > F) && (W = F / Math.max(j.width, j.height)), W < 1) if (typeof HTMLImageElement < "u" && w instanceof HTMLImageElement || typeof HTMLCanvasElement < "u" && w instanceof HTMLCanvasElement || typeof ImageBitmap < "u" && w instanceof ImageBitmap || typeof VideoFrame < "u" && w instanceof VideoFrame) {
      const oe = Math.floor(W * j.width), he = Math.floor(W * j.height);
      u === void 0 && (u = v(oe, he));
      const D = _ ? v(oe, he) : u;
      return D.width = oe, D.height = he, D.getContext("2d").drawImage(w, 0, 0, oe, he), Re("WebGLRenderer: Texture has been resized from (" + j.width + "x" + j.height + ") to (" + oe + "x" + he + ")."), D;
    } else return "data" in w && Re("WebGLRenderer: Image in DataTexture is too big (" + j.width + "x" + j.height + ")."), w;
    return w;
  }
  function g(w) {
    return w.generateMipmaps;
  }
  function E(w) {
    r.generateMipmap(w);
  }
  function R(w) {
    return w.isWebGLCubeRenderTarget ? r.TEXTURE_CUBE_MAP : w.isWebGL3DRenderTarget ? r.TEXTURE_3D : w.isWebGLArrayRenderTarget || w.isCompressedArrayTexture ? r.TEXTURE_2D_ARRAY : r.TEXTURE_2D;
  }
  function y(w, _, F, W, j, oe = !1) {
    if (w !== null) {
      if (r[w] !== void 0) return r[w];
      Re("WebGLRenderer: Attempt to use non-existing WebGL internal format '" + w + "'");
    }
    let he;
    W && (he = e.get("EXT_texture_norm16"), he || Re("WebGLRenderer: Unable to use normalized textures without EXT_texture_norm16 extension"));
    let D = _;
    if (_ === r.RED && (F === r.FLOAT && (D = r.R32F), F === r.HALF_FLOAT && (D = r.R16F), F === r.UNSIGNED_BYTE && (D = r.R8), F === r.UNSIGNED_SHORT && he && (D = he.R16_EXT), F === r.SHORT && he && (D = he.R16_SNORM_EXT)), _ === r.RED_INTEGER && (F === r.UNSIGNED_BYTE && (D = r.R8UI), F === r.UNSIGNED_SHORT && (D = r.R16UI), F === r.UNSIGNED_INT && (D = r.R32UI), F === r.BYTE && (D = r.R8I), F === r.SHORT && (D = r.R16I), F === r.INT && (D = r.R32I)), _ === r.RG && (F === r.FLOAT && (D = r.RG32F), F === r.HALF_FLOAT && (D = r.RG16F), F === r.UNSIGNED_BYTE && (D = r.RG8), F === r.UNSIGNED_SHORT && he && (D = he.RG16_EXT), F === r.SHORT && he && (D = he.RG16_SNORM_EXT)), _ === r.RG_INTEGER && (F === r.UNSIGNED_BYTE && (D = r.RG8UI), F === r.UNSIGNED_SHORT && (D = r.RG16UI), F === r.UNSIGNED_INT && (D = r.RG32UI), F === r.BYTE && (D = r.RG8I), F === r.SHORT && (D = r.RG16I), F === r.INT && (D = r.RG32I)), _ === r.RGB_INTEGER && (F === r.UNSIGNED_BYTE && (D = r.RGB8UI), F === r.UNSIGNED_SHORT && (D = r.RGB16UI), F === r.UNSIGNED_INT && (D = r.RGB32UI), F === r.BYTE && (D = r.RGB8I), F === r.SHORT && (D = r.RGB16I), F === r.INT && (D = r.RGB32I)), _ === r.RGBA_INTEGER && (F === r.UNSIGNED_BYTE && (D = r.RGBA8UI), F === r.UNSIGNED_SHORT && (D = r.RGBA16UI), F === r.UNSIGNED_INT && (D = r.RGBA32UI), F === r.BYTE && (D = r.RGBA8I), F === r.SHORT && (D = r.RGBA16I), F === r.INT && (D = r.RGBA32I)), _ === r.RGB && (F === r.UNSIGNED_SHORT && he && (D = he.RGB16_EXT), F === r.SHORT && he && (D = he.RGB16_SNORM_EXT), F === r.UNSIGNED_INT_5_9_9_9_REV && (D = r.RGB9_E5), F === r.UNSIGNED_INT_10F_11F_11F_REV && (D = r.R11F_G11F_B10F)), _ === r.RGBA) {
      const se = oe ? ca : Ze.getTransfer(j);
      F === r.FLOAT && (D = r.RGBA32F), F === r.HALF_FLOAT && (D = r.RGBA16F), F === r.UNSIGNED_BYTE && (D = se === ot ? r.SRGB8_ALPHA8 : r.RGBA8), F === r.UNSIGNED_SHORT && he && (D = he.RGBA16_EXT), F === r.SHORT && he && (D = he.RGBA16_SNORM_EXT), F === r.UNSIGNED_SHORT_4_4_4_4 && (D = r.RGBA4), F === r.UNSIGNED_SHORT_5_5_5_1 && (D = r.RGB5_A1);
    }
    return (D === r.R16F || D === r.R32F || D === r.RG16F || D === r.RG32F || D === r.RGBA16F || D === r.RGBA32F) && e.get("EXT_color_buffer_float"), D;
  }
  function S(w, _) {
    let F;
    return w ? _ === null || _ === Ni || _ === ns ? F = r.DEPTH24_STENCIL8 : _ === oi ? F = r.DEPTH32F_STENCIL8 : _ === rs && (F = r.DEPTH24_STENCIL8, Re("DepthTexture: 16 bit depth attachment is not supported with stencil. Using 24-bit attachment.")) : _ === null || _ === Ni || _ === ns ? F = r.DEPTH_COMPONENT24 : _ === oi ? F = r.DEPTH_COMPONENT32F : _ === rs && (F = r.DEPTH_COMPONENT16), F;
  }
  function T(w, _) {
    return g(w) === !0 || w.isFramebufferTexture && w.minFilter !== Rt && w.minFilter !== Pt ? Math.log2(Math.max(_.width, _.height)) + 1 : w.mipmaps !== void 0 && w.mipmaps.length > 0 ? w.mipmaps.length : w.isCompressedTexture && Array.isArray(w.image) ? _.mipmaps.length : 1;
  }
  function A(w) {
    const _ = w.target;
    _.removeEventListener("dispose", A), b(_), _.isVideoTexture && h.delete(_), _.isHTMLTexture && d.delete(_);
  }
  function x(w) {
    const _ = w.target;
    _.removeEventListener("dispose", x), O(_);
  }
  function b(w) {
    const _ = i.get(w);
    if (_.__webglInit === void 0) return;
    const F = w.source, W = p.get(F);
    if (W) {
      const j = W[_.__cacheKey];
      j.usedTimes--, j.usedTimes === 0 && N(w), Object.keys(W).length === 0 && p.delete(F);
    }
    i.remove(w);
  }
  function N(w) {
    const _ = i.get(w);
    r.deleteTexture(_.__webglTexture);
    const F = w.source, W = p.get(F);
    delete W[_.__cacheKey], a.memory.textures--;
  }
  function O(w) {
    const _ = i.get(w);
    if (w.depthTexture && (w.depthTexture.dispose(), i.remove(w.depthTexture)), w.isWebGLCubeRenderTarget) for (let W = 0; W < 6; W++) {
      if (Array.isArray(_.__webglFramebuffer[W])) for (let j = 0; j < _.__webglFramebuffer[W].length; j++) r.deleteFramebuffer(_.__webglFramebuffer[W][j]);
      else r.deleteFramebuffer(_.__webglFramebuffer[W]);
      _.__webglDepthbuffer && r.deleteRenderbuffer(_.__webglDepthbuffer[W]);
    }
    else {
      if (Array.isArray(_.__webglFramebuffer)) for (let W = 0; W < _.__webglFramebuffer.length; W++) r.deleteFramebuffer(_.__webglFramebuffer[W]);
      else r.deleteFramebuffer(_.__webglFramebuffer);
      if (_.__webglDepthbuffer && r.deleteRenderbuffer(_.__webglDepthbuffer), _.__webglMultisampledFramebuffer && r.deleteFramebuffer(_.__webglMultisampledFramebuffer), _.__webglColorRenderbuffer) for (let W = 0; W < _.__webglColorRenderbuffer.length; W++) _.__webglColorRenderbuffer[W] && r.deleteRenderbuffer(_.__webglColorRenderbuffer[W]);
      _.__webglDepthRenderbuffer && r.deleteRenderbuffer(_.__webglDepthRenderbuffer);
    }
    const F = w.textures;
    for (let W = 0, j = F.length; W < j; W++) {
      const oe = i.get(F[W]);
      oe.__webglTexture && (r.deleteTexture(oe.__webglTexture), a.memory.textures--), i.remove(F[W]);
    }
    i.remove(w);
  }
  let k = 0;
  function q() {
    k = 0;
  }
  function B() {
    return k;
  }
  function G(w) {
    k = w;
  }
  function Z() {
    const w = k;
    return w >= n.maxTextures && Re("WebGLTextures: Trying to use " + (w + 1) + " texture units while this GPU supports only " + n.maxTextures), k += 1, w;
  }
  function X(w) {
    const _ = [];
    return _.push(w.wrapS), _.push(w.wrapT), _.push(w.wrapR || 0), _.push(w.magFilter), _.push(w.minFilter), _.push(w.anisotropy), _.push(w.internalFormat), _.push(w.format), _.push(w.type), _.push(w.generateMipmaps), _.push(w.premultiplyAlpha), _.push(w.flipY), _.push(w.unpackAlignment), _.push(w.colorSpace), _.join();
  }
  function te(w, _) {
    const F = i.get(w);
    if (w.isVideoTexture && C(w), w.isRenderTargetTexture === !1 && w.isExternalTexture !== !0 && w.version > 0 && F.__version !== w.version) {
      const W = w.image;
      if (W === null) Re("WebGLRenderer: Texture marked for update but no image data found.");
      else if (W.complete === !1) Re("WebGLRenderer: Texture marked for update but image is incomplete");
      else {
        le(F, w, _);
        return;
      }
    } else w.isExternalTexture && (F.__webglTexture = w.sourceTexture ? w.sourceTexture : null);
    t.bindTexture(r.TEXTURE_2D, F.__webglTexture, r.TEXTURE0 + _);
  }
  function V(w, _) {
    const F = i.get(w);
    if (w.isRenderTargetTexture === !1 && w.version > 0 && F.__version !== w.version) {
      le(F, w, _);
      return;
    } else w.isExternalTexture && (F.__webglTexture = w.sourceTexture ? w.sourceTexture : null);
    t.bindTexture(r.TEXTURE_2D_ARRAY, F.__webglTexture, r.TEXTURE0 + _);
  }
  function K(w, _) {
    const F = i.get(w);
    if (w.isRenderTargetTexture === !1 && w.version > 0 && F.__version !== w.version) {
      le(F, w, _);
      return;
    }
    t.bindTexture(r.TEXTURE_3D, F.__webglTexture, r.TEXTURE0 + _);
  }
  function ee(w, _) {
    const F = i.get(w);
    if (w.isCubeDepthTexture !== !0 && w.version > 0 && F.__version !== w.version) {
      Oe(F, w, _);
      return;
    }
    t.bindTexture(r.TEXTURE_CUBE_MAP, F.__webglTexture, r.TEXTURE0 + _);
  }
  const Ue = { [_n]: r.REPEAT, [Ci]: r.CLAMP_TO_EDGE, [na]: r.MIRRORED_REPEAT }, ue = { [Rt]: r.NEAREST, [Iu]: r.NEAREST_MIPMAP_NEAREST, [Yn]: r.NEAREST_MIPMAP_LINEAR, [Pt]: r.LINEAR, [Ys]: r.LINEAR_MIPMAP_NEAREST, [Yi]: r.LINEAR_MIPMAP_LINEAR }, Ye = { [Bp]: r.NEVER, [Gp]: r.ALWAYS, [Vp]: r.LESS, [Gl]: r.LEQUAL, [Hp]: r.EQUAL, [Wl]: r.GEQUAL, [kp]: r.GREATER, [zp]: r.NOTEQUAL };
  function ke(w, _) {
    if (_.type === oi && e.has("OES_texture_float_linear") === !1 && (_.magFilter === Pt || _.magFilter === Ys || _.magFilter === Yn || _.magFilter === Yi || _.minFilter === Pt || _.minFilter === Ys || _.minFilter === Yn || _.minFilter === Yi) && Re("WebGLRenderer: Unable to use linear filtering with floating point textures. OES_texture_float_linear not supported on this device."), r.texParameteri(w, r.TEXTURE_WRAP_S, Ue[_.wrapS]), r.texParameteri(w, r.TEXTURE_WRAP_T, Ue[_.wrapT]), (w === r.TEXTURE_3D || w === r.TEXTURE_2D_ARRAY) && r.texParameteri(w, r.TEXTURE_WRAP_R, Ue[_.wrapR]), r.texParameteri(w, r.TEXTURE_MAG_FILTER, ue[_.magFilter]), r.texParameteri(w, r.TEXTURE_MIN_FILTER, ue[_.minFilter]), _.compareFunction && (r.texParameteri(w, r.TEXTURE_COMPARE_MODE, r.COMPARE_REF_TO_TEXTURE), r.texParameteri(w, r.TEXTURE_COMPARE_FUNC, Ye[_.compareFunction])), e.has("EXT_texture_filter_anisotropic") === !0) {
      if (_.magFilter === Rt || _.minFilter !== Yn && _.minFilter !== Yi || _.type === oi && e.has("OES_texture_float_linear") === !1) return;
      if (_.anisotropy > 1 || i.get(_).__currentAnisotropy) {
        const F = e.get("EXT_texture_filter_anisotropic");
        r.texParameterf(w, F.TEXTURE_MAX_ANISOTROPY_EXT, Math.min(_.anisotropy, n.getMaxAnisotropy())), i.get(_).__currentAnisotropy = _.anisotropy;
      }
    }
  }
  function je(w, _) {
    let F = !1;
    w.__webglInit === void 0 && (w.__webglInit = !0, _.addEventListener("dispose", A));
    const W = _.source;
    let j = p.get(W);
    j === void 0 && (j = {}, p.set(W, j));
    const oe = X(_);
    if (oe !== w.__cacheKey) {
      j[oe] === void 0 && (j[oe] = { texture: r.createTexture(), usedTimes: 0 }, a.memory.textures++, F = !0), j[oe].usedTimes++;
      const he = j[w.__cacheKey];
      he !== void 0 && (j[w.__cacheKey].usedTimes--, he.usedTimes === 0 && N(_)), w.__cacheKey = oe, w.__webglTexture = j[oe].texture;
    }
    return F;
  }
  function Y(w, _, F) {
    return Math.floor(Math.floor(w / F) / _);
  }
  function $(w, _, F, W) {
    const j = w.updateRanges;
    if (j.length === 0) t.texSubImage2D(r.TEXTURE_2D, 0, 0, 0, _.width, _.height, F, W, _.data);
    else {
      j.sort((ce, Me) => ce.start - Me.start);
      let oe = 0;
      for (let ce = 1; ce < j.length; ce++) {
        const Me = j[oe], ie = j[ce], ge = Me.start + Me.count, Se = Y(ie.start, _.width, 4), Pe = Y(Me.start, _.width, 4);
        ie.start <= ge + 1 && Se === Pe && Y(ie.start + ie.count - 1, _.width, 4) === Se ? Me.count = Math.max(Me.count, ie.start + ie.count - Me.start) : (++oe, j[oe] = ie);
      }
      j.length = oe + 1;
      const he = t.getParameter(r.UNPACK_ROW_LENGTH), D = t.getParameter(r.UNPACK_SKIP_PIXELS), se = t.getParameter(r.UNPACK_SKIP_ROWS);
      t.pixelStorei(r.UNPACK_ROW_LENGTH, _.width);
      for (let ce = 0, Me = j.length; ce < Me; ce++) {
        const ie = j[ce], ge = Math.floor(ie.start / 4), Se = Math.ceil(ie.count / 4), Pe = ge % _.width, Ie = Math.floor(ge / _.width), U = Se;
        t.pixelStorei(r.UNPACK_SKIP_PIXELS, Pe), t.pixelStorei(r.UNPACK_SKIP_ROWS, Ie), t.texSubImage2D(r.TEXTURE_2D, 0, Pe, Ie, U, 1, F, W, _.data);
      }
      w.clearUpdateRanges(), t.pixelStorei(r.UNPACK_ROW_LENGTH, he), t.pixelStorei(r.UNPACK_SKIP_PIXELS, D), t.pixelStorei(r.UNPACK_SKIP_ROWS, se);
    }
  }
  function le(w, _, F) {
    let W = r.TEXTURE_2D;
    (_.isDataArrayTexture || _.isCompressedArrayTexture) && (W = r.TEXTURE_2D_ARRAY), _.isData3DTexture && (W = r.TEXTURE_3D);
    const j = je(w, _), oe = _.source;
    t.bindTexture(W, w.__webglTexture, r.TEXTURE0 + F);
    const he = i.get(oe);
    if (oe.version !== he.__version || j === !0) {
      if (t.activeTexture(r.TEXTURE0 + F), !(typeof ImageBitmap < "u" && _.image instanceof ImageBitmap)) {
        const Q = Ze.getPrimaries(Ze.workingColorSpace), re = _.colorSpace === pr ? null : Ze.getPrimaries(_.colorSpace), _e = _.colorSpace === pr || Q === re ? r.NONE : r.BROWSER_DEFAULT_WEBGL;
        t.pixelStorei(r.UNPACK_FLIP_Y_WEBGL, _.flipY), t.pixelStorei(r.UNPACK_PREMULTIPLY_ALPHA_WEBGL, _.premultiplyAlpha), t.pixelStorei(r.UNPACK_COLORSPACE_CONVERSION_WEBGL, _e);
      }
      t.pixelStorei(r.UNPACK_ALIGNMENT, _.unpackAlignment);
      let D = m(_.image, !1, n.maxTextureSize);
      D = xt(_, D);
      const se = s.convert(_.format, _.colorSpace), ce = s.convert(_.type);
      let Me = y(_.internalFormat, se, ce, _.normalized, _.colorSpace, _.isVideoTexture);
      ke(W, _);
      let ie;
      const ge = _.mipmaps, Se = _.isVideoTexture !== !0, Pe = he.__version === void 0 || j === !0, Ie = oe.dataReady, U = T(_, D);
      if (_.isDepthTexture) Me = S(_.format === Cr, _.type), Pe && (Se ? t.texStorage2D(r.TEXTURE_2D, 1, Me, D.width, D.height) : t.texImage2D(r.TEXTURE_2D, 0, Me, D.width, D.height, 0, se, ce, null));
      else if (_.isDataTexture) if (ge.length > 0) {
        Se && Pe && t.texStorage2D(r.TEXTURE_2D, U, Me, ge[0].width, ge[0].height);
        for (let Q = 0, re = ge.length; Q < re; Q++) ie = ge[Q], Se ? Ie && t.texSubImage2D(r.TEXTURE_2D, Q, 0, 0, ie.width, ie.height, se, ce, ie.data) : t.texImage2D(r.TEXTURE_2D, Q, Me, ie.width, ie.height, 0, se, ce, ie.data);
        _.generateMipmaps = !1;
      } else Se ? (Pe && t.texStorage2D(r.TEXTURE_2D, U, Me, D.width, D.height), Ie && $(_, D, se, ce)) : t.texImage2D(r.TEXTURE_2D, 0, Me, D.width, D.height, 0, se, ce, D.data);
      else if (_.isCompressedTexture) if (_.isCompressedArrayTexture) {
        Se && Pe && t.texStorage3D(r.TEXTURE_2D_ARRAY, U, Me, ge[0].width, ge[0].height, D.depth);
        for (let Q = 0, re = ge.length; Q < re; Q++) if (ie = ge[Q], _.format !== li) if (se !== null) if (Se) {
          if (Ie) if (_.layerUpdates.size > 0) {
            const _e = Jc(ie.width, ie.height, _.format, _.type);
            for (const ae of _.layerUpdates) {
              const J = ie.data.subarray(ae * _e / ie.data.BYTES_PER_ELEMENT, (ae + 1) * _e / ie.data.BYTES_PER_ELEMENT);
              t.compressedTexSubImage3D(r.TEXTURE_2D_ARRAY, Q, 0, 0, ae, ie.width, ie.height, 1, se, J);
            }
          } else t.compressedTexSubImage3D(r.TEXTURE_2D_ARRAY, Q, 0, 0, 0, ie.width, ie.height, D.depth, se, ie.data);
        } else t.compressedTexImage3D(r.TEXTURE_2D_ARRAY, Q, Me, ie.width, ie.height, D.depth, 0, ie.data, 0, 0);
        else Re("WebGLRenderer: Attempt to load unsupported compressed texture format in .uploadTexture()");
        else Se ? Ie && t.texSubImage3D(r.TEXTURE_2D_ARRAY, Q, 0, 0, 0, ie.width, ie.height, D.depth, se, ce, ie.data) : t.texImage3D(r.TEXTURE_2D_ARRAY, Q, Me, ie.width, ie.height, D.depth, 0, se, ce, ie.data);
        _.layerUpdates.size > 0 && _.clearLayerUpdates();
      } else {
        Se && Pe && t.texStorage2D(r.TEXTURE_2D, U, Me, ge[0].width, ge[0].height);
        for (let Q = 0, re = ge.length; Q < re; Q++) ie = ge[Q], _.format !== li ? se !== null ? Se ? Ie && t.compressedTexSubImage2D(r.TEXTURE_2D, Q, 0, 0, ie.width, ie.height, se, ie.data) : t.compressedTexImage2D(r.TEXTURE_2D, Q, Me, ie.width, ie.height, 0, ie.data) : Re("WebGLRenderer: Attempt to load unsupported compressed texture format in .uploadTexture()") : Se ? Ie && t.texSubImage2D(r.TEXTURE_2D, Q, 0, 0, ie.width, ie.height, se, ce, ie.data) : t.texImage2D(r.TEXTURE_2D, Q, Me, ie.width, ie.height, 0, se, ce, ie.data);
      }
      else if (_.isDataArrayTexture) if (Se) {
        if (Pe && t.texStorage3D(r.TEXTURE_2D_ARRAY, U, Me, D.width, D.height, D.depth), Ie) if (_.layerUpdates.size > 0) {
          const Q = Jc(D.width, D.height, _.format, _.type);
          for (const re of _.layerUpdates) {
            const _e = D.data.subarray(re * Q / D.data.BYTES_PER_ELEMENT, (re + 1) * Q / D.data.BYTES_PER_ELEMENT);
            t.texSubImage3D(r.TEXTURE_2D_ARRAY, 0, 0, 0, re, D.width, D.height, 1, se, ce, _e);
          }
          _.clearLayerUpdates();
        } else t.texSubImage3D(r.TEXTURE_2D_ARRAY, 0, 0, 0, 0, D.width, D.height, D.depth, se, ce, D.data);
      } else t.texImage3D(r.TEXTURE_2D_ARRAY, 0, Me, D.width, D.height, D.depth, 0, se, ce, D.data);
      else if (_.isData3DTexture) Se ? (Pe && t.texStorage3D(r.TEXTURE_3D, U, Me, D.width, D.height, D.depth), Ie && t.texSubImage3D(r.TEXTURE_3D, 0, 0, 0, 0, D.width, D.height, D.depth, se, ce, D.data)) : t.texImage3D(r.TEXTURE_3D, 0, Me, D.width, D.height, D.depth, 0, se, ce, D.data);
      else if (_.isFramebufferTexture) {
        if (Pe) if (Se) t.texStorage2D(r.TEXTURE_2D, U, Me, D.width, D.height);
        else {
          let Q = D.width, re = D.height;
          for (let _e = 0; _e < U; _e++) t.texImage2D(r.TEXTURE_2D, _e, Me, Q, re, 0, se, ce, null), Q >>= 1, re >>= 1;
        }
      } else if (_.isHTMLTexture) {
        if ("texElementImage2D" in r) {
          const Q = r.canvas;
          if (Q.hasAttribute("layoutsubtree") || Q.setAttribute("layoutsubtree", "true"), D.parentNode !== Q) {
            Q.appendChild(D), d.add(_), Q.onpaint = (re) => {
              const _e = re.changedElements;
              for (const ae of d) _e.includes(ae.image) && (ae.needsUpdate = !0);
            }, Q.requestPaint();
            return;
          }
          if (r.texElementImage2D.length === 3) r.texElementImage2D(r.TEXTURE_2D, r.RGBA8, D);
          else {
            const re = r.RGBA, _e = r.RGBA, ae = r.UNSIGNED_BYTE;
            r.texElementImage2D(r.TEXTURE_2D, 0, re, _e, ae, D);
          }
          r.texParameteri(r.TEXTURE_2D, r.TEXTURE_MIN_FILTER, r.LINEAR), r.texParameteri(r.TEXTURE_2D, r.TEXTURE_WRAP_S, r.CLAMP_TO_EDGE), r.texParameteri(r.TEXTURE_2D, r.TEXTURE_WRAP_T, r.CLAMP_TO_EDGE);
        }
      } else if (ge.length > 0) {
        if (Se && Pe) {
          const Q = Qe(ge[0]);
          t.texStorage2D(r.TEXTURE_2D, U, Me, Q.width, Q.height);
        }
        for (let Q = 0, re = ge.length; Q < re; Q++) ie = ge[Q], Se ? Ie && t.texSubImage2D(r.TEXTURE_2D, Q, 0, 0, se, ce, ie) : t.texImage2D(r.TEXTURE_2D, Q, Me, se, ce, ie);
        _.generateMipmaps = !1;
      } else if (Se) {
        if (Pe) {
          const Q = Qe(D);
          t.texStorage2D(r.TEXTURE_2D, U, Me, Q.width, Q.height);
        }
        Ie && t.texSubImage2D(r.TEXTURE_2D, 0, 0, 0, se, ce, D);
      } else t.texImage2D(r.TEXTURE_2D, 0, Me, se, ce, D);
      g(_) && E(W), he.__version = oe.version, _.onUpdate && _.onUpdate(_);
    }
    w.__version = _.version;
  }
  function Oe(w, _, F) {
    if (_.image.length !== 6) return;
    const W = je(w, _), j = _.source;
    t.bindTexture(r.TEXTURE_CUBE_MAP, w.__webglTexture, r.TEXTURE0 + F);
    const oe = i.get(j);
    if (j.version !== oe.__version || W === !0) {
      t.activeTexture(r.TEXTURE0 + F);
      const he = Ze.getPrimaries(Ze.workingColorSpace), D = _.colorSpace === pr ? null : Ze.getPrimaries(_.colorSpace), se = _.colorSpace === pr || he === D ? r.NONE : r.BROWSER_DEFAULT_WEBGL;
      t.pixelStorei(r.UNPACK_FLIP_Y_WEBGL, _.flipY), t.pixelStorei(r.UNPACK_PREMULTIPLY_ALPHA_WEBGL, _.premultiplyAlpha), t.pixelStorei(r.UNPACK_ALIGNMENT, _.unpackAlignment), t.pixelStorei(r.UNPACK_COLORSPACE_CONVERSION_WEBGL, se);
      const ce = _.isCompressedTexture || _.image[0].isCompressedTexture, Me = _.image[0] && _.image[0].isDataTexture, ie = [];
      for (let J = 0; J < 6; J++) !ce && !Me ? ie[J] = m(_.image[J], !0, n.maxCubemapSize) : ie[J] = Me ? _.image[J].image : _.image[J], ie[J] = xt(_, ie[J]);
      const ge = ie[0], Se = s.convert(_.format, _.colorSpace), Pe = s.convert(_.type), Ie = y(_.internalFormat, Se, Pe, _.normalized, _.colorSpace), U = _.isVideoTexture !== !0, Q = oe.__version === void 0 || W === !0, re = j.dataReady;
      let _e = T(_, ge);
      ke(r.TEXTURE_CUBE_MAP, _);
      let ae;
      if (ce) {
        U && Q && t.texStorage2D(r.TEXTURE_CUBE_MAP, _e, Ie, ge.width, ge.height);
        for (let J = 0; J < 6; J++) {
          ae = ie[J].mipmaps;
          for (let me = 0; me < ae.length; me++) {
            const pe = ae[me];
            _.format !== li ? Se !== null ? U ? re && t.compressedTexSubImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, me, 0, 0, pe.width, pe.height, Se, pe.data) : t.compressedTexImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, me, Ie, pe.width, pe.height, 0, pe.data) : Re("WebGLRenderer: Attempt to load unsupported compressed texture format in .setTextureCube()") : U ? re && t.texSubImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, me, 0, 0, pe.width, pe.height, Se, Pe, pe.data) : t.texImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, me, Ie, pe.width, pe.height, 0, Se, Pe, pe.data);
          }
        }
      } else {
        if (ae = _.mipmaps, U && Q) {
          ae.length > 0 && _e++;
          const J = Qe(ie[0]);
          t.texStorage2D(r.TEXTURE_CUBE_MAP, _e, Ie, J.width, J.height);
        }
        for (let J = 0; J < 6; J++) if (Me) {
          U ? re && t.texSubImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, 0, 0, 0, ie[J].width, ie[J].height, Se, Pe, ie[J].data) : t.texImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, 0, Ie, ie[J].width, ie[J].height, 0, Se, Pe, ie[J].data);
          for (let me = 0; me < ae.length; me++) {
            const pe = ae[me].image[J].image;
            U ? re && t.texSubImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, me + 1, 0, 0, pe.width, pe.height, Se, Pe, pe.data) : t.texImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, me + 1, Ie, pe.width, pe.height, 0, Se, Pe, pe.data);
          }
        } else {
          U ? re && t.texSubImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, 0, 0, 0, Se, Pe, ie[J]) : t.texImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, 0, Ie, Se, Pe, ie[J]);
          for (let me = 0; me < ae.length; me++) {
            const pe = ae[me];
            U ? re && t.texSubImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, me + 1, 0, 0, Se, Pe, pe.image[J]) : t.texImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + J, me + 1, Ie, Se, Pe, pe.image[J]);
          }
        }
      }
      g(_) && E(r.TEXTURE_CUBE_MAP), oe.__version = j.version, _.onUpdate && _.onUpdate(_);
    }
    w.__version = _.version;
  }
  function xe(w, _, F, W, j, oe) {
    const he = s.convert(F.format, F.colorSpace), D = s.convert(F.type), se = y(F.internalFormat, he, D, F.normalized, F.colorSpace), ce = i.get(_), Me = i.get(F);
    if (Me.__renderTarget = _, !ce.__hasExternalTextures) {
      const ie = Math.max(1, _.width >> oe), ge = Math.max(1, _.height >> oe);
      j === r.TEXTURE_3D || j === r.TEXTURE_2D_ARRAY ? t.texImage3D(j, oe, se, ie, ge, _.depth, 0, he, D, null) : t.texImage2D(j, oe, se, ie, ge, 0, he, D, null);
    }
    t.bindFramebuffer(r.FRAMEBUFFER, w), _t(_) ? o.framebufferTexture2DMultisampleEXT(r.FRAMEBUFFER, W, j, Me.__webglTexture, 0, tt(_)) : (j === r.TEXTURE_2D || j >= r.TEXTURE_CUBE_MAP_POSITIVE_X && j <= r.TEXTURE_CUBE_MAP_NEGATIVE_Z) && r.framebufferTexture2D(r.FRAMEBUFFER, W, j, Me.__webglTexture, oe), t.bindFramebuffer(r.FRAMEBUFFER, null);
  }
  function Te(w, _, F) {
    if (r.bindRenderbuffer(r.RENDERBUFFER, w), _.depthBuffer) {
      const W = _.depthTexture, j = W && W.isDepthTexture ? W.type : null, oe = S(_.stencilBuffer, j), he = _.stencilBuffer ? r.DEPTH_STENCIL_ATTACHMENT : r.DEPTH_ATTACHMENT;
      _t(_) ? o.renderbufferStorageMultisampleEXT(r.RENDERBUFFER, tt(_), oe, _.width, _.height) : F ? r.renderbufferStorageMultisample(r.RENDERBUFFER, tt(_), oe, _.width, _.height) : r.renderbufferStorage(r.RENDERBUFFER, oe, _.width, _.height), r.framebufferRenderbuffer(r.FRAMEBUFFER, he, r.RENDERBUFFER, w);
    } else {
      const W = _.textures;
      for (let j = 0; j < W.length; j++) {
        const oe = W[j], he = s.convert(oe.format, oe.colorSpace), D = s.convert(oe.type), se = y(oe.internalFormat, he, D, oe.normalized, oe.colorSpace);
        _t(_) ? o.renderbufferStorageMultisampleEXT(r.RENDERBUFFER, tt(_), se, _.width, _.height) : F ? r.renderbufferStorageMultisample(r.RENDERBUFFER, tt(_), se, _.width, _.height) : r.renderbufferStorage(r.RENDERBUFFER, se, _.width, _.height);
      }
    }
    r.bindRenderbuffer(r.RENDERBUFFER, null);
  }
  function He(w, _, F) {
    const W = _.isWebGLCubeRenderTarget === !0;
    if (t.bindFramebuffer(r.FRAMEBUFFER, w), !(_.depthTexture && _.depthTexture.isDepthTexture)) throw new Error("THREE.WebGLTextures: renderTarget.depthTexture must be an instance of THREE.DepthTexture.");
    const j = i.get(_.depthTexture);
    if (j.__renderTarget = _, (!j.__webglTexture || _.depthTexture.image.width !== _.width || _.depthTexture.image.height !== _.height) && (_.depthTexture.image.width = _.width, _.depthTexture.image.height = _.height, _.depthTexture.needsUpdate = !0), W) {
      if (j.__webglInit === void 0 && (j.__webglInit = !0, _.depthTexture.addEventListener("dispose", A)), j.__webglTexture === void 0) {
        j.__webglTexture = r.createTexture(), t.bindTexture(r.TEXTURE_CUBE_MAP, j.__webglTexture), ke(r.TEXTURE_CUBE_MAP, _.depthTexture);
        const ce = s.convert(_.depthTexture.format), Me = s.convert(_.depthTexture.type);
        let ie;
        _.depthTexture.format === Ji ? ie = r.DEPTH_COMPONENT24 : _.depthTexture.format === Cr && (ie = r.DEPTH24_STENCIL8);
        for (let ge = 0; ge < 6; ge++) r.texImage2D(r.TEXTURE_CUBE_MAP_POSITIVE_X + ge, 0, ie, _.width, _.height, 0, ce, Me, null);
      }
    } else te(_.depthTexture, 0);
    const oe = j.__webglTexture, he = tt(_), D = W ? r.TEXTURE_CUBE_MAP_POSITIVE_X + F : r.TEXTURE_2D, se = _.depthTexture.format === Cr ? r.DEPTH_STENCIL_ATTACHMENT : r.DEPTH_ATTACHMENT;
    if (_.depthTexture.format === Ji) _t(_) ? o.framebufferTexture2DMultisampleEXT(r.FRAMEBUFFER, se, D, oe, 0, he) : r.framebufferTexture2D(r.FRAMEBUFFER, se, D, oe, 0);
    else if (_.depthTexture.format === Cr) _t(_) ? o.framebufferTexture2DMultisampleEXT(r.FRAMEBUFFER, se, D, oe, 0, he) : r.framebufferTexture2D(r.FRAMEBUFFER, se, D, oe, 0);
    else throw new Error("THREE.WebGLTextures: Unknown depthTexture format.");
  }
  function Ke(w) {
    const _ = i.get(w), F = w.isWebGLCubeRenderTarget === !0;
    if (_.__boundDepthTexture !== w.depthTexture) {
      const W = w.depthTexture;
      if (_.__depthDisposeCallback && _.__depthDisposeCallback(), W) {
        const j = () => {
          delete _.__boundDepthTexture, delete _.__depthDisposeCallback, W.removeEventListener("dispose", j);
        };
        W.addEventListener("dispose", j), _.__depthDisposeCallback = j;
      }
      _.__boundDepthTexture = W;
    }
    if (w.depthTexture && !_.__autoAllocateDepthBuffer) if (F) for (let W = 0; W < 6; W++) He(_.__webglFramebuffer[W], w, W);
    else {
      const W = w.texture.mipmaps;
      W && W.length > 0 ? He(_.__webglFramebuffer[0], w, 0) : He(_.__webglFramebuffer, w, 0);
    }
    else if (F) {
      _.__webglDepthbuffer = [];
      for (let W = 0; W < 6; W++) if (t.bindFramebuffer(r.FRAMEBUFFER, _.__webglFramebuffer[W]), _.__webglDepthbuffer[W] === void 0) _.__webglDepthbuffer[W] = r.createRenderbuffer(), Te(_.__webglDepthbuffer[W], w, !1);
      else {
        const j = w.stencilBuffer ? r.DEPTH_STENCIL_ATTACHMENT : r.DEPTH_ATTACHMENT, oe = _.__webglDepthbuffer[W];
        r.bindRenderbuffer(r.RENDERBUFFER, oe), r.framebufferRenderbuffer(r.FRAMEBUFFER, j, r.RENDERBUFFER, oe);
      }
    } else {
      const W = w.texture.mipmaps;
      if (W && W.length > 0 ? t.bindFramebuffer(r.FRAMEBUFFER, _.__webglFramebuffer[0]) : t.bindFramebuffer(r.FRAMEBUFFER, _.__webglFramebuffer), _.__webglDepthbuffer === void 0) _.__webglDepthbuffer = r.createRenderbuffer(), Te(_.__webglDepthbuffer, w, !1);
      else {
        const j = w.stencilBuffer ? r.DEPTH_STENCIL_ATTACHMENT : r.DEPTH_ATTACHMENT, oe = _.__webglDepthbuffer;
        r.bindRenderbuffer(r.RENDERBUFFER, oe), r.framebufferRenderbuffer(r.FRAMEBUFFER, j, r.RENDERBUFFER, oe);
      }
    }
    t.bindFramebuffer(r.FRAMEBUFFER, null);
  }
  function Ve(w, _, F) {
    const W = i.get(w);
    _ !== void 0 && xe(W.__webglFramebuffer, w, w.texture, r.COLOR_ATTACHMENT0, r.TEXTURE_2D, 0), F !== void 0 && Ke(w);
  }
  function vt(w) {
    const _ = w.texture, F = i.get(w), W = i.get(_);
    w.addEventListener("dispose", x);
    const j = w.textures, oe = w.isWebGLCubeRenderTarget === !0, he = j.length > 1;
    if (he || (W.__webglTexture === void 0 && (W.__webglTexture = r.createTexture()), W.__version = _.version, a.memory.textures++), oe) {
      F.__webglFramebuffer = [];
      for (let D = 0; D < 6; D++) if (_.mipmaps && _.mipmaps.length > 0) {
        F.__webglFramebuffer[D] = [];
        for (let se = 0; se < _.mipmaps.length; se++) F.__webglFramebuffer[D][se] = r.createFramebuffer();
      } else F.__webglFramebuffer[D] = r.createFramebuffer();
    } else {
      if (_.mipmaps && _.mipmaps.length > 0) {
        F.__webglFramebuffer = [];
        for (let D = 0; D < _.mipmaps.length; D++) F.__webglFramebuffer[D] = r.createFramebuffer();
      } else F.__webglFramebuffer = r.createFramebuffer();
      if (he) for (let D = 0, se = j.length; D < se; D++) {
        const ce = i.get(j[D]);
        ce.__webglTexture === void 0 && (ce.__webglTexture = r.createTexture(), a.memory.textures++);
      }
      if (w.samples > 0 && _t(w) === !1) {
        F.__webglMultisampledFramebuffer = r.createFramebuffer(), F.__webglColorRenderbuffer = [], t.bindFramebuffer(r.FRAMEBUFFER, F.__webglMultisampledFramebuffer);
        for (let D = 0; D < j.length; D++) {
          const se = j[D];
          F.__webglColorRenderbuffer[D] = r.createRenderbuffer(), r.bindRenderbuffer(r.RENDERBUFFER, F.__webglColorRenderbuffer[D]);
          const ce = s.convert(se.format, se.colorSpace), Me = s.convert(se.type), ie = y(se.internalFormat, ce, Me, se.normalized, se.colorSpace, w.isXRRenderTarget === !0), ge = tt(w);
          r.renderbufferStorageMultisample(r.RENDERBUFFER, ge, ie, w.width, w.height), r.framebufferRenderbuffer(r.FRAMEBUFFER, r.COLOR_ATTACHMENT0 + D, r.RENDERBUFFER, F.__webglColorRenderbuffer[D]);
        }
        r.bindRenderbuffer(r.RENDERBUFFER, null), w.depthBuffer && (F.__webglDepthRenderbuffer = r.createRenderbuffer(), Te(F.__webglDepthRenderbuffer, w, !0)), t.bindFramebuffer(r.FRAMEBUFFER, null);
      }
    }
    if (oe) {
      t.bindTexture(r.TEXTURE_CUBE_MAP, W.__webglTexture), ke(r.TEXTURE_CUBE_MAP, _);
      for (let D = 0; D < 6; D++) if (_.mipmaps && _.mipmaps.length > 0) for (let se = 0; se < _.mipmaps.length; se++) xe(F.__webglFramebuffer[D][se], w, _, r.COLOR_ATTACHMENT0, r.TEXTURE_CUBE_MAP_POSITIVE_X + D, se);
      else xe(F.__webglFramebuffer[D], w, _, r.COLOR_ATTACHMENT0, r.TEXTURE_CUBE_MAP_POSITIVE_X + D, 0);
      g(_) && E(r.TEXTURE_CUBE_MAP), t.unbindTexture();
    } else if (he) {
      for (let D = 0, se = j.length; D < se; D++) {
        const ce = j[D], Me = i.get(ce);
        let ie = r.TEXTURE_2D;
        (w.isWebGL3DRenderTarget || w.isWebGLArrayRenderTarget) && (ie = w.isWebGL3DRenderTarget ? r.TEXTURE_3D : r.TEXTURE_2D_ARRAY), t.bindTexture(ie, Me.__webglTexture), ke(ie, ce), xe(F.__webglFramebuffer, w, ce, r.COLOR_ATTACHMENT0 + D, ie, 0), g(ce) && E(ie);
      }
      t.unbindTexture();
    } else {
      let D = r.TEXTURE_2D;
      if ((w.isWebGL3DRenderTarget || w.isWebGLArrayRenderTarget) && (D = w.isWebGL3DRenderTarget ? r.TEXTURE_3D : r.TEXTURE_2D_ARRAY), t.bindTexture(D, W.__webglTexture), ke(D, _), _.mipmaps && _.mipmaps.length > 0) for (let se = 0; se < _.mipmaps.length; se++) xe(F.__webglFramebuffer[se], w, _, r.COLOR_ATTACHMENT0, D, se);
      else xe(F.__webglFramebuffer, w, _, r.COLOR_ATTACHMENT0, D, 0);
      g(_) && E(D), t.unbindTexture();
    }
    w.depthBuffer && Ke(w);
  }
  function Mt(w) {
    const _ = w.textures;
    for (let F = 0, W = _.length; F < W; F++) {
      const j = _[F];
      if (g(j)) {
        const oe = R(w), he = i.get(j).__webglTexture;
        t.bindTexture(oe, he), E(oe), t.unbindTexture();
      }
    }
  }
  const dt = [], Je = [];
  function At(w) {
    if (w.samples > 0) {
      if (_t(w) === !1) {
        const _ = w.textures, F = w.width, W = w.height;
        let j = r.COLOR_BUFFER_BIT;
        const oe = w.stencilBuffer ? r.DEPTH_STENCIL_ATTACHMENT : r.DEPTH_ATTACHMENT, he = i.get(w), D = _.length > 1;
        if (D) for (let ce = 0; ce < _.length; ce++) t.bindFramebuffer(r.FRAMEBUFFER, he.__webglMultisampledFramebuffer), r.framebufferRenderbuffer(r.FRAMEBUFFER, r.COLOR_ATTACHMENT0 + ce, r.RENDERBUFFER, null), t.bindFramebuffer(r.FRAMEBUFFER, he.__webglFramebuffer), r.framebufferTexture2D(r.DRAW_FRAMEBUFFER, r.COLOR_ATTACHMENT0 + ce, r.TEXTURE_2D, null, 0);
        t.bindFramebuffer(r.READ_FRAMEBUFFER, he.__webglMultisampledFramebuffer);
        const se = w.texture.mipmaps;
        se && se.length > 0 ? t.bindFramebuffer(r.DRAW_FRAMEBUFFER, he.__webglFramebuffer[0]) : t.bindFramebuffer(r.DRAW_FRAMEBUFFER, he.__webglFramebuffer);
        for (let ce = 0; ce < _.length; ce++) {
          if (w.resolveDepthBuffer && (w.depthBuffer && (j |= r.DEPTH_BUFFER_BIT), w.stencilBuffer && w.resolveStencilBuffer && (j |= r.STENCIL_BUFFER_BIT)), D) {
            r.framebufferRenderbuffer(r.READ_FRAMEBUFFER, r.COLOR_ATTACHMENT0, r.RENDERBUFFER, he.__webglColorRenderbuffer[ce]);
            const Me = i.get(_[ce]).__webglTexture;
            r.framebufferTexture2D(r.DRAW_FRAMEBUFFER, r.COLOR_ATTACHMENT0, r.TEXTURE_2D, Me, 0);
          }
          r.blitFramebuffer(0, 0, F, W, 0, 0, F, W, j, r.NEAREST), l === !0 && (dt.length = 0, Je.length = 0, dt.push(r.COLOR_ATTACHMENT0 + ce), w.depthBuffer && w.storeMultisampledDepthBuffer === !1 && (dt.push(oe), Je.push(oe), r.invalidateFramebuffer(r.DRAW_FRAMEBUFFER, Je)), r.invalidateFramebuffer(r.READ_FRAMEBUFFER, dt));
        }
        if (t.bindFramebuffer(r.READ_FRAMEBUFFER, null), t.bindFramebuffer(r.DRAW_FRAMEBUFFER, null), D) for (let ce = 0; ce < _.length; ce++) {
          t.bindFramebuffer(r.FRAMEBUFFER, he.__webglMultisampledFramebuffer), r.framebufferRenderbuffer(r.FRAMEBUFFER, r.COLOR_ATTACHMENT0 + ce, r.RENDERBUFFER, he.__webglColorRenderbuffer[ce]);
          const Me = i.get(_[ce]).__webglTexture;
          t.bindFramebuffer(r.FRAMEBUFFER, he.__webglFramebuffer), r.framebufferTexture2D(r.DRAW_FRAMEBUFFER, r.COLOR_ATTACHMENT0 + ce, r.TEXTURE_2D, Me, 0);
        }
        t.bindFramebuffer(r.DRAW_FRAMEBUFFER, he.__webglMultisampledFramebuffer);
      } else if (w.depthBuffer && w.storeMultisampledDepthBuffer === !1 && l) {
        const _ = w.stencilBuffer ? r.DEPTH_STENCIL_ATTACHMENT : r.DEPTH_ATTACHMENT;
        r.invalidateFramebuffer(r.DRAW_FRAMEBUFFER, [_]);
      }
    }
  }
  function tt(w) {
    return Math.min(n.maxSamples, w.samples);
  }
  function _t(w) {
    const _ = i.get(w);
    return w.samples > 0 && e.has("WEBGL_multisampled_render_to_texture") === !0 && _.__useRenderToTexture !== !1;
  }
  function C(w) {
    const _ = a.render.frame;
    h.get(w) !== _ && (h.set(w, _), w.update());
  }
  function xt(w, _) {
    const F = w.colorSpace, W = w.format, j = w.type;
    return w.isCompressedTexture === !0 || w.isVideoTexture === !0 || F !== ei && F !== pr && (Ze.getTransfer(F) === ot ? (W !== li || j !== $t) && Re("WebGLTextures: sRGB encoded textures have to use RGBAFormat and UnsignedByteType.") : Be("WebGLTextures: Unsupported texture color space:", F)), _;
  }
  function Qe(w) {
    return typeof HTMLImageElement < "u" && w instanceof HTMLImageElement ? (c.width = w.naturalWidth || w.width, c.height = w.naturalHeight || w.height) : typeof VideoFrame < "u" && w instanceof VideoFrame ? (c.width = w.displayWidth, c.height = w.displayHeight) : (c.width = w.width, c.height = w.height), c;
  }
  this.allocateTextureUnit = Z, this.resetTextureUnits = q, this.getTextureUnits = B, this.setTextureUnits = G, this.setTexture2D = te, this.setTexture2DArray = V, this.setTexture3D = K, this.setTextureCube = ee, this.rebindTextures = Ve, this.setupRenderTarget = vt, this.updateRenderTargetMipmap = Mt, this.updateMultisampleRenderTarget = At, this.setupDepthRenderbuffer = Ke, this.setupFrameBufferTexture = xe, this.useMultisampledRTT = _t, this.isReversedDepthBuffer = function() {
    return t.buffers.depth.getReversed();
  };
}
function vx(r, e) {
  function t(i, n = pr) {
    let s;
    const a = Ze.getTransfer(n);
    if (i === $t) return r.UNSIGNED_BYTE;
    if (i === Ol) return r.UNSIGNED_SHORT_4_4_4_4;
    if (i === Fl) return r.UNSIGNED_SHORT_5_5_5_1;
    if (i === Du) return r.UNSIGNED_INT_5_9_9_9_REV;
    if (i === Ou) return r.UNSIGNED_INT_10F_11F_11F_REV;
    if (i === Nu) return r.BYTE;
    if (i === Uu) return r.SHORT;
    if (i === rs) return r.UNSIGNED_SHORT;
    if (i === Dl) return r.INT;
    if (i === Ni) return r.UNSIGNED_INT;
    if (i === oi) return r.FLOAT;
    if (i === Ui) return r.HALF_FLOAT;
    if (i === Fu) return r.ALPHA;
    if (i === Bu) return r.RGB;
    if (i === li) return r.RGBA;
    if (i === Ji) return r.DEPTH_COMPONENT;
    if (i === Cr) return r.DEPTH_STENCIL;
    if (i === Bl) return r.RED;
    if (i === Vl) return r.RED_INTEGER;
    if (i === Ur) return r.RG;
    if (i === Hl) return r.RG_INTEGER;
    if (i === kl) return r.RGBA_INTEGER;
    if (i === Ks || i === Zs || i === $s || i === Qs) if (a === ot) if (s = e.get("WEBGL_compressed_texture_s3tc_srgb"), s !== null) {
      if (i === Ks) return s.COMPRESSED_SRGB_S3TC_DXT1_EXT;
      if (i === Zs) return s.COMPRESSED_SRGB_ALPHA_S3TC_DXT1_EXT;
      if (i === $s) return s.COMPRESSED_SRGB_ALPHA_S3TC_DXT3_EXT;
      if (i === Qs) return s.COMPRESSED_SRGB_ALPHA_S3TC_DXT5_EXT;
    } else return null;
    else if (s = e.get("WEBGL_compressed_texture_s3tc"), s !== null) {
      if (i === Ks) return s.COMPRESSED_RGB_S3TC_DXT1_EXT;
      if (i === Zs) return s.COMPRESSED_RGBA_S3TC_DXT1_EXT;
      if (i === $s) return s.COMPRESSED_RGBA_S3TC_DXT3_EXT;
      if (i === Qs) return s.COMPRESSED_RGBA_S3TC_DXT5_EXT;
    } else return null;
    if (i === Go || i === Wo || i === Xo || i === jo) if (s = e.get("WEBGL_compressed_texture_pvrtc"), s !== null) {
      if (i === Go) return s.COMPRESSED_RGB_PVRTC_4BPPV1_IMG;
      if (i === Wo) return s.COMPRESSED_RGB_PVRTC_2BPPV1_IMG;
      if (i === Xo) return s.COMPRESSED_RGBA_PVRTC_4BPPV1_IMG;
      if (i === jo) return s.COMPRESSED_RGBA_PVRTC_2BPPV1_IMG;
    } else return null;
    if (i === qo || i === Yo || i === Ko || i === Zo || i === $o || i === sa || i === Qo) if (s = e.get("WEBGL_compressed_texture_etc"), s !== null) {
      if (i === qo || i === Yo) return a === ot ? s.COMPRESSED_SRGB8_ETC2 : s.COMPRESSED_RGB8_ETC2;
      if (i === Ko) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ETC2_EAC : s.COMPRESSED_RGBA8_ETC2_EAC;
      if (i === Zo) return s.COMPRESSED_R11_EAC;
      if (i === $o) return s.COMPRESSED_SIGNED_R11_EAC;
      if (i === sa) return s.COMPRESSED_RG11_EAC;
      if (i === Qo) return s.COMPRESSED_SIGNED_RG11_EAC;
    } else return null;
    if (i === Jo || i === el || i === tl || i === il || i === rl || i === nl || i === sl || i === al || i === ol || i === ll || i === cl || i === hl || i === ul || i === dl) if (s = e.get("WEBGL_compressed_texture_astc"), s !== null) {
      if (i === Jo) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_4x4_KHR : s.COMPRESSED_RGBA_ASTC_4x4_KHR;
      if (i === el) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_5x4_KHR : s.COMPRESSED_RGBA_ASTC_5x4_KHR;
      if (i === tl) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_5x5_KHR : s.COMPRESSED_RGBA_ASTC_5x5_KHR;
      if (i === il) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_6x5_KHR : s.COMPRESSED_RGBA_ASTC_6x5_KHR;
      if (i === rl) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_6x6_KHR : s.COMPRESSED_RGBA_ASTC_6x6_KHR;
      if (i === nl) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_8x5_KHR : s.COMPRESSED_RGBA_ASTC_8x5_KHR;
      if (i === sl) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_8x6_KHR : s.COMPRESSED_RGBA_ASTC_8x6_KHR;
      if (i === al) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_8x8_KHR : s.COMPRESSED_RGBA_ASTC_8x8_KHR;
      if (i === ol) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_10x5_KHR : s.COMPRESSED_RGBA_ASTC_10x5_KHR;
      if (i === ll) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_10x6_KHR : s.COMPRESSED_RGBA_ASTC_10x6_KHR;
      if (i === cl) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_10x8_KHR : s.COMPRESSED_RGBA_ASTC_10x8_KHR;
      if (i === hl) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_10x10_KHR : s.COMPRESSED_RGBA_ASTC_10x10_KHR;
      if (i === ul) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_12x10_KHR : s.COMPRESSED_RGBA_ASTC_12x10_KHR;
      if (i === dl) return a === ot ? s.COMPRESSED_SRGB8_ALPHA8_ASTC_12x12_KHR : s.COMPRESSED_RGBA_ASTC_12x12_KHR;
    } else return null;
    if (i === pl || i === fl || i === ml) if (s = e.get("EXT_texture_compression_bptc"), s !== null) {
      if (i === pl) return a === ot ? s.COMPRESSED_SRGB_ALPHA_BPTC_UNORM_EXT : s.COMPRESSED_RGBA_BPTC_UNORM_EXT;
      if (i === fl) return s.COMPRESSED_RGB_BPTC_SIGNED_FLOAT_EXT;
      if (i === ml) return s.COMPRESSED_RGB_BPTC_UNSIGNED_FLOAT_EXT;
    } else return null;
    if (i === gl || i === _l || i === aa || i === vl) if (s = e.get("EXT_texture_compression_rgtc"), s !== null) {
      if (i === gl) return s.COMPRESSED_RED_RGTC1_EXT;
      if (i === _l) return s.COMPRESSED_SIGNED_RED_RGTC1_EXT;
      if (i === aa) return s.COMPRESSED_RED_GREEN_RGTC2_EXT;
      if (i === vl) return s.COMPRESSED_SIGNED_RED_GREEN_RGTC2_EXT;
    } else return null;
    return i === ns ? r.UNSIGNED_INT_24_8 : r[i] !== void 0 ? r[i] : null;
  }
  return { convert: t };
}
const xx = `
void main() {

	gl_Position = vec4( position, 1.0 );

}`, Mx = `
uniform sampler2DArray depthColor;
uniform float depthWidth;
uniform float depthHeight;

void main() {

	vec2 coord = vec2( gl_FragCoord.x / depthWidth, gl_FragCoord.y / depthHeight );

	if ( coord.x >= 1.0 ) {

		gl_FragDepth = texture( depthColor, vec3( coord.x - 1.0, coord.y, 1 ) ).r;

	} else {

		gl_FragDepth = texture( depthColor, vec3( coord.x, coord.y, 0 ) ).r;

	}

}`;
class yx {
  constructor() {
    this.texture = null, this.mesh = null, this.depthNear = 0, this.depthFar = 0;
  }
  init(e, t) {
    if (this.texture === null) {
      const i = new rd(e.texture);
      (e.depthNear !== t.depthNear || e.depthFar !== t.depthFar) && (this.depthNear = e.depthNear, this.depthFar = e.depthFar), this.texture = i;
    }
  }
  getMesh(e) {
    if (this.texture !== null && this.mesh === null) {
      const t = e.cameras[0].viewport, i = new xi({ vertexShader: xx, fragmentShader: Mx, uniforms: { depthColor: { value: this.texture }, depthWidth: { value: t.z }, depthHeight: { value: t.w } } });
      this.mesh = new Wt(new Sa(20, 20), i);
    }
    return this.mesh;
  }
  reset() {
    this.texture = null, this.mesh = null;
  }
  getDepthTexture() {
    return this.texture;
  }
}
class Sx extends mr {
  constructor(e, t) {
    super();
    const i = this;
    let n = null, s = 1, a = null, o = "local-floor", l = 1, c = null, h = null, d = null, u = null, p = null, f = null;
    const v = typeof XRWebGLBinding < "u", m = new yx(), g = {}, E = t.getContextAttributes();
    let R = null, y = null;
    const S = [], T = [], A = new We();
    let x = null, b = null;
    const N = new zt();
    N.viewport = new gt();
    const O = new zt();
    O.viewport = new gt();
    const k = [N, O], q = new mm();
    let B = null, G = null;
    this.cameraAutoUpdate = !0, this.enabled = !1, this.isPresenting = !1, this.getController = function(Y) {
      let $ = S[Y];
      return $ === void 0 && ($ = new Ha(), S[Y] = $), $.getTargetRaySpace();
    }, this.getControllerGrip = function(Y) {
      let $ = S[Y];
      return $ === void 0 && ($ = new Ha(), S[Y] = $), $.getGripSpace();
    }, this.getHand = function(Y) {
      let $ = S[Y];
      return $ === void 0 && ($ = new Ha(), S[Y] = $), $.getHandSpace();
    };
    function Z(Y) {
      const $ = T.indexOf(Y.inputSource);
      if ($ === -1) return;
      const le = S[$];
      le !== void 0 && (le.update(Y.inputSource, Y.frame, c || a), le.dispatchEvent({ type: Y.type, data: Y.inputSource }));
    }
    function X() {
      n.removeEventListener("select", Z), n.removeEventListener("selectstart", Z), n.removeEventListener("selectend", Z), n.removeEventListener("squeeze", Z), n.removeEventListener("squeezestart", Z), n.removeEventListener("squeezeend", Z), n.removeEventListener("end", X), n.removeEventListener("inputsourceschange", te);
      for (let Y = 0; Y < S.length; Y++) {
        const $ = T[Y];
        $ !== null && (T[Y] = null, S[Y].disconnect($));
      }
      B = null, G = null, m.reset();
      for (const Y in g) delete g[Y];
      if (e.setRenderTarget(R), p = null, u = null, d = null, n = null, y = null, je.stop(), i.isPresenting = !1, e.setPixelRatio(x), e.setSize(A.width, A.height, !1), b !== null) {
        const Y = b.camera;
        Y.fov = b.fov, Y.zoom = b.zoom, Y.updateProjectionMatrix(), b = null;
      }
      i.dispatchEvent({ type: "sessionend" });
    }
    this.setFramebufferScaleFactor = function(Y) {
      s = Y, i.isPresenting === !0 && Re("WebXRManager: Cannot change framebuffer scale while presenting.");
    }, this.setReferenceSpaceType = function(Y) {
      o = Y, i.isPresenting === !0 && Re("WebXRManager: Cannot change reference space type while presenting.");
    }, this.getReferenceSpace = function() {
      return c || a;
    }, this.setReferenceSpace = function(Y) {
      c = Y;
    }, this.getBaseLayer = function() {
      return u !== null ? u : p;
    }, this.getBinding = function() {
      return d === null && v && (d = new XRWebGLBinding(n, t)), d;
    }, this.getFrame = function() {
      return f;
    }, this.getSession = function() {
      return n;
    }, this.setSession = async function(Y) {
      if (n = Y, n !== null) {
        if (R = e.getRenderTarget(), n.addEventListener("select", Z), n.addEventListener("selectstart", Z), n.addEventListener("selectend", Z), n.addEventListener("squeeze", Z), n.addEventListener("squeezestart", Z), n.addEventListener("squeezeend", Z), n.addEventListener("end", X), n.addEventListener("inputsourceschange", te), E.xrCompatible !== !0 && await t.makeXRCompatible(), x = e.getPixelRatio(), e.getSize(A), v && "createProjectionLayer" in XRWebGLBinding.prototype) {
          let $ = null, le = null, Oe = null;
          E.depth && (Oe = E.stencil ? t.DEPTH24_STENCIL8 : t.DEPTH_COMPONENT24, $ = E.stencil ? Cr : Ji, le = E.stencil ? ns : Ni);
          const xe = { colorFormat: t.RGBA8, depthFormat: Oe, scaleFactor: s };
          d = this.getBinding(), u = d.createProjectionLayer(xe), n.updateRenderState({ layers: [u] }), e.setPixelRatio(1), e.setSize(u.textureWidth, u.textureHeight, !1), y = new _i(u.textureWidth, u.textureHeight, { format: li, type: $t, depthTexture: new cs(u.textureWidth, u.textureHeight, le, void 0, void 0, void 0, void 0, void 0, void 0, $), stencilBuffer: E.stencil, colorSpace: e.outputColorSpace, samples: E.antialias ? 4 : 0, resolveDepthBuffer: u.ignoreDepthValues === !1, resolveStencilBuffer: u.ignoreDepthValues === !1, storeMultisampledDepthBuffer: u.ignoreDepthValues === !1, storeMultisampledStencilBuffer: u.ignoreDepthValues === !1 });
        } else {
          const $ = { antialias: E.antialias, alpha: !0, depth: E.depth, stencil: E.stencil, framebufferScaleFactor: s };
          p = new XRWebGLLayer(n, t, $), n.updateRenderState({ baseLayer: p }), e.setPixelRatio(1), e.setSize(p.framebufferWidth, p.framebufferHeight, !1), y = new _i(p.framebufferWidth, p.framebufferHeight, { format: li, type: $t, colorSpace: e.outputColorSpace, stencilBuffer: E.stencil, resolveDepthBuffer: p.ignoreDepthValues === !1, resolveStencilBuffer: p.ignoreDepthValues === !1, storeMultisampledDepthBuffer: p.ignoreDepthValues === !1, storeMultisampledStencilBuffer: p.ignoreDepthValues === !1 });
        }
        y.isXRRenderTarget = !0, this.setFoveation(l), c = null, a = await n.requestReferenceSpace(o), je.setContext(n), je.start(), i.isPresenting = !0, i.dispatchEvent({ type: "sessionstart" });
      }
    }, this.getEnvironmentBlendMode = function() {
      if (n !== null) return n.environmentBlendMode;
    }, this.getDepthTexture = function() {
      return m.getDepthTexture();
    };
    function te(Y) {
      for (let $ = 0; $ < Y.removed.length; $++) {
        const le = Y.removed[$], Oe = T.indexOf(le);
        Oe >= 0 && (T[Oe] = null, S[Oe].disconnect(le));
      }
      for (let $ = 0; $ < Y.added.length; $++) {
        const le = Y.added[$];
        let Oe = T.indexOf(le);
        if (Oe === -1) {
          for (let Te = 0; Te < S.length; Te++) if (Te >= T.length) {
            T.push(le), Oe = Te;
            break;
          } else if (T[Te] === null) {
            T[Te] = le, Oe = Te;
            break;
          }
          if (Oe === -1) break;
        }
        const xe = S[Oe];
        xe && xe.connect(le);
      }
    }
    const V = new P(), K = new P();
    function ee(Y, $, le) {
      V.setFromMatrixPosition($.matrixWorld), K.setFromMatrixPosition(le.matrixWorld);
      const Oe = V.distanceTo(K), xe = $.projectionMatrix.elements, Te = le.projectionMatrix.elements, He = xe[14] / (xe[10] - 1), Ke = xe[14] / (xe[10] + 1), Ve = (xe[9] + 1) / xe[5], vt = (xe[9] - 1) / xe[5], Mt = (xe[8] - 1) / xe[0], dt = (Te[8] + 1) / Te[0], Je = He * Mt, At = He * dt, tt = Oe / (-Mt + dt), _t = tt * -Mt;
      if ($.matrixWorld.decompose(Y.position, Y.quaternion, Y.scale), Y.translateX(_t), Y.translateZ(tt), Y.matrixWorld.compose(Y.position, Y.quaternion, Y.scale), Y.matrixWorldInverse.copy(Y.matrixWorld).invert(), xe[10] === -1) Y.projectionMatrix.copy($.projectionMatrix), Y.projectionMatrixInverse.copy($.projectionMatrixInverse);
      else {
        const C = He + tt, xt = Ke + tt, Qe = Je - _t, w = At + (Oe - _t), _ = Ve * Ke / xt * C, F = vt * Ke / xt * C;
        Y.projectionMatrix.makePerspective(Qe, w, _, F, C, xt), Y.projectionMatrixInverse.copy(Y.projectionMatrix).invert();
      }
    }
    function Ue(Y, $) {
      $ === null ? Y.matrixWorld.copy(Y.matrix) : Y.matrixWorld.multiplyMatrices($.matrixWorld, Y.matrix), Y.matrixWorldInverse.copy(Y.matrixWorld).invert();
    }
    this.updateCamera = function(Y) {
      if (n === null) return;
      let $ = Y.near, le = Y.far;
      m.texture !== null && (m.depthNear > 0 && ($ = m.depthNear), m.depthFar > 0 && (le = m.depthFar)), q.near = O.near = N.near = $, q.far = O.far = N.far = le, (B !== q.near || G !== q.far) && (n.updateRenderState({ depthNear: q.near, depthFar: q.far }), B = q.near, G = q.far), q.layers.mask = Y.layers.mask | 6, N.layers.mask = q.layers.mask & -5, O.layers.mask = q.layers.mask & -3;
      const Oe = Y.parent, xe = q.cameras;
      Ue(q, Oe);
      for (let Te = 0; Te < xe.length; Te++) Ue(xe[Te], Oe);
      xe.length === 2 ? ee(q, N, O) : q.projectionMatrix.copy(N.projectionMatrix), b === null && Y.isPerspectiveCamera && (b = { camera: Y, fov: Y.fov, zoom: Y.zoom }), ue(Y, q, Oe);
    };
    function ue(Y, $, le) {
      le === null ? Y.matrix.copy($.matrixWorld) : (Y.matrix.copy(le.matrixWorld), Y.matrix.invert(), Y.matrix.multiply($.matrixWorld)), Y.matrix.decompose(Y.position, Y.quaternion, Y.scale), Y.updateMatrixWorld(!0), Y.projectionMatrix.copy($.projectionMatrix), Y.projectionMatrixInverse.copy($.projectionMatrixInverse), Y.isPerspectiveCamera && (Y.fov = vn * 2 * Math.atan(1 / Y.projectionMatrix.elements[5]), Y.zoom = 1);
    }
    this.getCamera = function() {
      return q;
    }, this.getFoveation = function() {
      if (!(u === null && p === null)) return l;
    }, this.setFoveation = function(Y) {
      l = Y, u !== null && (u.fixedFoveation = Y), p !== null && p.fixedFoveation !== void 0 && (p.fixedFoveation = Y);
    }, this.hasDepthSensing = function() {
      return m.texture !== null;
    }, this.getDepthSensingMesh = function() {
      return m.getMesh(q);
    }, this.getCameraTexture = function(Y) {
      return g[Y];
    };
    let Ye = null;
    function ke(Y, $) {
      if (h = $.getViewerPose(c || a), f = $, h !== null) {
        const le = h.views;
        p !== null && (e.setRenderTargetFramebuffer(y, p.framebuffer), e.setRenderTarget(y));
        let Oe = !1;
        le.length !== q.cameras.length && (q.cameras.length = 0, Oe = !0);
        for (let Te = 0; Te < le.length; Te++) {
          const He = le[Te];
          let Ke = null;
          if (p !== null) Ke = p.getViewport(He);
          else {
            const vt = d.getViewSubImage(u, He);
            Ke = vt.viewport, Te === 0 && (e.setRenderTargetTextures(y, vt.colorTexture, vt.depthStencilTexture), e.setRenderTarget(y));
          }
          let Ve = k[Te];
          Ve === void 0 && (Ve = new zt(), Ve.layers.enable(Te), Ve.viewport = new gt(), k[Te] = Ve), Ve.matrix.fromArray(He.transform.matrix), Ve.matrix.decompose(Ve.position, Ve.quaternion, Ve.scale), Ve.projectionMatrix.fromArray(He.projectionMatrix), Ve.projectionMatrixInverse.copy(Ve.projectionMatrix).invert(), Ve.viewport.set(Ke.x, Ke.y, Ke.width, Ke.height), Te === 0 && (q.matrix.copy(Ve.matrix), q.matrix.decompose(q.position, q.quaternion, q.scale)), Oe === !0 && q.cameras.push(Ve);
        }
        const xe = n.enabledFeatures;
        if (xe && xe.includes("depth-sensing") && n.depthUsage == "gpu-optimized" && v) {
          d = i.getBinding();
          const Te = d.getDepthInformation(le[0]);
          Te && Te.isValid && Te.texture && m.init(Te, n.renderState);
        }
        if (xe && xe.includes("camera-access") && v) {
          e.state.unbindTexture(), d = i.getBinding();
          for (let Te = 0; Te < le.length; Te++) {
            const He = le[Te].camera;
            if (He) {
              let Ke = g[He];
              Ke || (Ke = new rd(), g[He] = Ke);
              const Ve = d.getCameraImage(He);
              Ke.sourceTexture = Ve;
            }
          }
        }
      }
      for (let le = 0; le < S.length; le++) {
        const Oe = T[le], xe = S[le];
        Oe !== null && xe !== void 0 && xe.update(Oe, $, c || a);
      }
      Ye && Ye(Y, $), $.detectedPlanes && i.dispatchEvent({ type: "planesdetected", data: $ }), f = null;
    }
    const je = new pd();
    je.setAnimationLoop(ke), this.setAnimationLoop = function(Y) {
      Ye = Y;
    }, this.dispose = function() {
    };
  }
}
const Tx = new Fe(), Md = new De();
Md.set(-1, 0, 0, 0, 1, 0, 0, 0, 1);
function Ex(r, e) {
  function t(m, g) {
    m.matrixAutoUpdate === !0 && m.updateMatrix(), g.value.copy(m.matrix);
  }
  function i(m, g) {
    g.color.getRGB(m.fogColor.value, nd(r)), g.isFog ? (m.fogNear.value = g.near, m.fogFar.value = g.far) : g.isFogExp2 && (m.fogDensity.value = g.density);
  }
  function n(m, g, E, R, y) {
    g.isNodeMaterial ? g.uniformsNeedUpdate = !1 : g.isMeshBasicMaterial ? s(m, g) : g.isMeshLambertMaterial ? (s(m, g), g.envMap && (m.envMapIntensity.value = g.envMapIntensity)) : g.isMeshToonMaterial ? (s(m, g), d(m, g)) : g.isMeshPhongMaterial ? (s(m, g), h(m, g), g.envMap && (m.envMapIntensity.value = g.envMapIntensity)) : g.isMeshStandardMaterial ? (s(m, g), u(m, g), g.isMeshPhysicalMaterial && p(m, g, y)) : g.isMeshMatcapMaterial ? (s(m, g), f(m, g)) : g.isMeshDepthMaterial ? s(m, g) : g.isMeshDistanceMaterial ? (s(m, g), v(m, g)) : g.isMeshNormalMaterial ? s(m, g) : g.isLineBasicMaterial ? (a(m, g), g.isLineDashedMaterial && o(m, g)) : g.isPointsMaterial ? l(m, g, E, R) : g.isSpriteMaterial ? c(m, g) : g.isShadowMaterial ? (m.color.value.copy(g.color), m.opacity.value = g.opacity) : g.isShaderMaterial && (g.uniformsNeedUpdate = !1);
  }
  function s(m, g) {
    m.opacity.value = g.opacity, g.color && m.diffuse.value.copy(g.color), g.emissive && m.emissive.value.copy(g.emissive).multiplyScalar(g.emissiveIntensity), g.map && (m.map.value = g.map, t(g.map, m.mapTransform)), g.alphaMap && (m.alphaMap.value = g.alphaMap, t(g.alphaMap, m.alphaMapTransform)), g.bumpMap && (m.bumpMap.value = g.bumpMap, t(g.bumpMap, m.bumpMapTransform), m.bumpScale.value = g.bumpScale, g.side === Gt && (m.bumpScale.value *= -1)), g.normalMap && (m.normalMap.value = g.normalMap, t(g.normalMap, m.normalMapTransform), m.normalScale.value.copy(g.normalScale), g.side === Gt && m.normalScale.value.negate()), g.displacementMap && (m.displacementMap.value = g.displacementMap, t(g.displacementMap, m.displacementMapTransform), m.displacementScale.value = g.displacementScale, m.displacementBias.value = g.displacementBias), g.emissiveMap && (m.emissiveMap.value = g.emissiveMap, t(g.emissiveMap, m.emissiveMapTransform)), g.specularMap && (m.specularMap.value = g.specularMap, t(g.specularMap, m.specularMapTransform)), g.alphaTest > 0 && (m.alphaTest.value = g.alphaTest);
    const E = e.get(g), R = E.envMap, y = E.envMapRotation;
    R && (m.envMap.value = R, m.envMapRotation.value.setFromMatrix4(Tx.makeRotationFromEuler(y)).transpose(), R.isCubeTexture && R.isRenderTargetTexture === !1 && m.envMapRotation.value.premultiply(Md), m.reflectivity.value = g.reflectivity, m.ior.value = g.ior, m.refractionRatio.value = g.refractionRatio), g.lightMap && (m.lightMap.value = g.lightMap, m.lightMapIntensity.value = g.lightMapIntensity, t(g.lightMap, m.lightMapTransform)), g.aoMap && (m.aoMap.value = g.aoMap, m.aoMapIntensity.value = g.aoMapIntensity, t(g.aoMap, m.aoMapTransform));
  }
  function a(m, g) {
    m.diffuse.value.copy(g.color), m.opacity.value = g.opacity, g.map && (m.map.value = g.map, t(g.map, m.mapTransform));
  }
  function o(m, g) {
    m.dashSize.value = g.dashSize, m.totalSize.value = g.dashSize + g.gapSize, m.scale.value = g.scale;
  }
  function l(m, g, E, R) {
    m.diffuse.value.copy(g.color), m.opacity.value = g.opacity, m.size.value = g.size * E, m.scale.value = R * 0.5, g.map && (m.map.value = g.map, t(g.map, m.uvTransform)), g.alphaMap && (m.alphaMap.value = g.alphaMap, t(g.alphaMap, m.alphaMapTransform)), g.alphaTest > 0 && (m.alphaTest.value = g.alphaTest);
  }
  function c(m, g) {
    m.diffuse.value.copy(g.color), m.opacity.value = g.opacity, m.rotation.value = g.rotation, g.map && (m.map.value = g.map, t(g.map, m.mapTransform)), g.alphaMap && (m.alphaMap.value = g.alphaMap, t(g.alphaMap, m.alphaMapTransform)), g.alphaTest > 0 && (m.alphaTest.value = g.alphaTest);
  }
  function h(m, g) {
    m.specular.value.copy(g.specular), m.shininess.value = Math.max(g.shininess, 1e-4);
  }
  function d(m, g) {
    g.gradientMap && (m.gradientMap.value = g.gradientMap);
  }
  function u(m, g) {
    m.metalness.value = g.metalness, g.metalnessMap && (m.metalnessMap.value = g.metalnessMap, t(g.metalnessMap, m.metalnessMapTransform)), m.roughness.value = g.roughness, g.roughnessMap && (m.roughnessMap.value = g.roughnessMap, t(g.roughnessMap, m.roughnessMapTransform)), g.envMap && (m.envMapIntensity.value = g.envMapIntensity);
  }
  function p(m, g, E) {
    m.ior.value = g.ior, g.sheen > 0 && (m.sheenColor.value.copy(g.sheenColor).multiplyScalar(g.sheen), m.sheenRoughness.value = g.sheenRoughness, g.sheenColorMap && (m.sheenColorMap.value = g.sheenColorMap, t(g.sheenColorMap, m.sheenColorMapTransform)), g.sheenRoughnessMap && (m.sheenRoughnessMap.value = g.sheenRoughnessMap, t(g.sheenRoughnessMap, m.sheenRoughnessMapTransform))), g.clearcoat > 0 && (m.clearcoat.value = g.clearcoat, m.clearcoatRoughness.value = g.clearcoatRoughness, g.clearcoatMap && (m.clearcoatMap.value = g.clearcoatMap, t(g.clearcoatMap, m.clearcoatMapTransform)), g.clearcoatRoughnessMap && (m.clearcoatRoughnessMap.value = g.clearcoatRoughnessMap, t(g.clearcoatRoughnessMap, m.clearcoatRoughnessMapTransform)), g.clearcoatNormalMap && (m.clearcoatNormalMap.value = g.clearcoatNormalMap, t(g.clearcoatNormalMap, m.clearcoatNormalMapTransform), m.clearcoatNormalScale.value.copy(g.clearcoatNormalScale), g.side === Gt && m.clearcoatNormalScale.value.negate())), g.dispersion > 0 && (m.dispersion.value = g.dispersion), g.retroreflectivity > 0 && (m.retroreflectivity.value = g.retroreflectivity), g.iridescence > 0 && (m.iridescence.value = g.iridescence, m.iridescenceIOR.value = g.iridescenceIOR, m.iridescenceThicknessMinimum.value = g.iridescenceThicknessRange[0], m.iridescenceThicknessMaximum.value = g.iridescenceThicknessRange[1], g.iridescenceMap && (m.iridescenceMap.value = g.iridescenceMap, t(g.iridescenceMap, m.iridescenceMapTransform)), g.iridescenceThicknessMap && (m.iridescenceThicknessMap.value = g.iridescenceThicknessMap, t(g.iridescenceThicknessMap, m.iridescenceThicknessMapTransform))), g.transmission > 0 && (m.transmission.value = g.transmission, m.transmissionSamplerMap.value = E.texture, m.transmissionSamplerSize.value.set(E.width, E.height), g.transmissionMap && (m.transmissionMap.value = g.transmissionMap, t(g.transmissionMap, m.transmissionMapTransform)), m.thickness.value = g.thickness, g.thicknessMap && (m.thicknessMap.value = g.thicknessMap, t(g.thicknessMap, m.thicknessMapTransform)), m.attenuationDistance.value = g.attenuationDistance, m.attenuationColor.value.copy(g.attenuationColor)), g.anisotropy > 0 && (m.anisotropyVector.value.set(g.anisotropy * Math.cos(g.anisotropyRotation), g.anisotropy * Math.sin(g.anisotropyRotation)), g.anisotropyMap && (m.anisotropyMap.value = g.anisotropyMap, t(g.anisotropyMap, m.anisotropyMapTransform))), m.specularIntensity.value = g.specularIntensity, m.specularColor.value.copy(g.specularColor), g.specularColorMap && (m.specularColorMap.value = g.specularColorMap, t(g.specularColorMap, m.specularColorMapTransform)), g.specularIntensityMap && (m.specularIntensityMap.value = g.specularIntensityMap, t(g.specularIntensityMap, m.specularIntensityMapTransform));
  }
  function f(m, g) {
    g.matcap && (m.matcap.value = g.matcap);
  }
  function v(m, g) {
    const E = e.get(g).light;
    m.referencePosition.value.setFromMatrixPosition(E.matrixWorld), m.nearDistance.value = E.shadow.camera.near, m.farDistance.value = E.shadow.camera.far;
  }
  return { refreshFogUniforms: i, refreshMaterialUniforms: n };
}
function bx(r, e, t, i) {
  let n = {}, s = {}, a = [];
  const o = r.getParameter(r.MAX_UNIFORM_BUFFER_BINDINGS);
  function l(y, S) {
    const T = S.program;
    i.uniformBlockBinding(y, T);
  }
  function c(y, S) {
    let T = n[y.id];
    T === void 0 && (m(y), T = h(y), n[y.id] = T, y.addEventListener("dispose", E));
    const A = S.program;
    i.updateUBOMapping(y, A);
    const x = e.render.frame;
    s[y.id] !== x && (u(y), s[y.id] = x);
  }
  function h(y) {
    const S = d();
    y.__bindingPointIndex = S;
    const T = r.createBuffer(), A = y.__size, x = y.usage;
    return r.bindBuffer(r.UNIFORM_BUFFER, T), r.bufferData(r.UNIFORM_BUFFER, A, x), r.bindBuffer(r.UNIFORM_BUFFER, null), r.bindBufferBase(r.UNIFORM_BUFFER, S, T), T;
  }
  function d() {
    for (let y = 0; y < o; y++) if (a.indexOf(y) === -1) return a.push(y), y;
    return Be("WebGLRenderer: Maximum number of simultaneously usable uniforms groups reached."), 0;
  }
  function u(y) {
    const S = n[y.id], T = y.uniforms, A = y.__cache;
    r.bindBuffer(r.UNIFORM_BUFFER, S);
    for (let x = 0, b = T.length; x < b; x++) {
      const N = T[x];
      if (Array.isArray(N)) for (let O = 0, k = N.length; O < k; O++) p(N[O], x, O, A);
      else p(N, x, 0, A);
    }
    r.bindBuffer(r.UNIFORM_BUFFER, null);
  }
  function p(y, S, T, A) {
    if (v(y, S, T, A) === !0) {
      const x = y.__offset, b = y.value;
      if (Array.isArray(b)) {
        let N = 0;
        for (let O = 0; O < b.length; O++) {
          const k = b[O], q = g(k);
          f(k, y.__data, N), typeof k != "number" && typeof k != "boolean" && !k.isMatrix3 && !ArrayBuffer.isView(k) && (N += q.storage / Float32Array.BYTES_PER_ELEMENT);
        }
      } else f(b, y.__data, 0);
      r.bufferSubData(r.UNIFORM_BUFFER, x, y.__data);
    }
  }
  function f(y, S, T) {
    typeof y == "number" || typeof y == "boolean" ? S[0] = y : y.isMatrix3 ? (S[0] = y.elements[0], S[1] = y.elements[1], S[2] = y.elements[2], S[3] = 0, S[4] = y.elements[3], S[5] = y.elements[4], S[6] = y.elements[5], S[7] = 0, S[8] = y.elements[6], S[9] = y.elements[7], S[10] = y.elements[8], S[11] = 0) : ArrayBuffer.isView(y) ? S.set(new y.constructor(y.buffer, y.byteOffset, S.length)) : y.toArray(S, T);
  }
  function v(y, S, T, A) {
    const x = y.value, b = S + "_" + T;
    if (A[b] === void 0) return typeof x == "number" || typeof x == "boolean" ? A[b] = x : ArrayBuffer.isView(x) ? A[b] = x.slice() : A[b] = x.clone(), !0;
    {
      const N = A[b];
      if (typeof x == "number" || typeof x == "boolean") {
        if (N !== x) return A[b] = x, !0;
      } else {
        if (ArrayBuffer.isView(x)) return !0;
        if (N.equals(x) === !1) return N.copy(x), !0;
      }
    }
    return !1;
  }
  function m(y) {
    const S = y.uniforms;
    let T = 0;
    const A = 16;
    for (let b = 0, N = S.length; b < N; b++) {
      const O = Array.isArray(S[b]) ? S[b] : [S[b]];
      for (let k = 0, q = O.length; k < q; k++) {
        const B = O[k], G = Array.isArray(B.value) ? B.value : [B.value];
        for (let Z = 0, X = G.length; Z < X; Z++) {
          const te = G[Z], V = g(te), K = T % A, ee = K % V.boundary, Ue = K + ee;
          T += ee, Ue !== 0 && A - Ue < V.storage && (T += A - Ue), B.__data = new Float32Array(V.storage / Float32Array.BYTES_PER_ELEMENT), B.__offset = T, T += V.storage;
        }
      }
    }
    const x = T % A;
    return x > 0 && (T += A - x), y.__size = T, y.__cache = {}, this;
  }
  function g(y) {
    const S = { boundary: 0, storage: 0 };
    return typeof y == "number" || typeof y == "boolean" ? (S.boundary = 4, S.storage = 4) : y.isVector2 ? (S.boundary = 8, S.storage = 8) : y.isVector3 || y.isColor ? (S.boundary = 16, S.storage = 12) : y.isVector4 ? (S.boundary = 16, S.storage = 16) : y.isMatrix3 ? (S.boundary = 48, S.storage = 48) : y.isMatrix4 ? (S.boundary = 64, S.storage = 64) : y.isTexture ? Re("WebGLRenderer: Texture samplers can not be part of an uniforms group.") : ArrayBuffer.isView(y) ? (S.boundary = 16, S.storage = y.byteLength) : Re("WebGLRenderer: Unsupported uniform value type.", y), S;
  }
  function E(y) {
    const S = y.target;
    S.removeEventListener("dispose", E);
    const T = a.indexOf(S.__bindingPointIndex);
    a.splice(T, 1), r.deleteBuffer(n[S.id]), delete n[S.id], delete s[S.id];
  }
  function R() {
    for (const y in n) r.deleteBuffer(n[y]);
    a = [], n = {}, s = {};
  }
  return { bind: l, update: c, dispose: R };
}
const wx = new Uint16Array([12469, 15057, 12620, 14925, 13266, 14620, 13807, 14376, 14323, 13990, 14545, 13625, 14713, 13328, 14840, 12882, 14931, 12528, 14996, 12233, 15039, 11829, 15066, 11525, 15080, 11295, 15085, 10976, 15082, 10705, 15073, 10495, 13880, 14564, 13898, 14542, 13977, 14430, 14158, 14124, 14393, 13732, 14556, 13410, 14702, 12996, 14814, 12596, 14891, 12291, 14937, 11834, 14957, 11489, 14958, 11194, 14943, 10803, 14921, 10506, 14893, 10278, 14858, 9960, 14484, 14039, 14487, 14025, 14499, 13941, 14524, 13740, 14574, 13468, 14654, 13106, 14743, 12678, 14818, 12344, 14867, 11893, 14889, 11509, 14893, 11180, 14881, 10751, 14852, 10428, 14812, 10128, 14765, 9754, 14712, 9466, 14764, 13480, 14764, 13475, 14766, 13440, 14766, 13347, 14769, 13070, 14786, 12713, 14816, 12387, 14844, 11957, 14860, 11549, 14868, 11215, 14855, 10751, 14825, 10403, 14782, 10044, 14729, 9651, 14666, 9352, 14599, 9029, 14967, 12835, 14966, 12831, 14963, 12804, 14954, 12723, 14936, 12564, 14917, 12347, 14900, 11958, 14886, 11569, 14878, 11247, 14859, 10765, 14828, 10401, 14784, 10011, 14727, 9600, 14660, 9289, 14586, 8893, 14508, 8533, 15111, 12234, 15110, 12234, 15104, 12216, 15092, 12156, 15067, 12010, 15028, 11776, 14981, 11500, 14942, 11205, 14902, 10752, 14861, 10393, 14812, 9991, 14752, 9570, 14682, 9252, 14603, 8808, 14519, 8445, 14431, 8145, 15209, 11449, 15208, 11451, 15202, 11451, 15190, 11438, 15163, 11384, 15117, 11274, 15055, 10979, 14994, 10648, 14932, 10343, 14871, 9936, 14803, 9532, 14729, 9218, 14645, 8742, 14556, 8381, 14461, 8020, 14365, 7603, 15273, 10603, 15272, 10607, 15267, 10619, 15256, 10631, 15231, 10614, 15182, 10535, 15118, 10389, 15042, 10167, 14963, 9787, 14883, 9447, 14800, 9115, 14710, 8665, 14615, 8318, 14514, 7911, 14411, 7507, 14279, 7198, 15314, 9675, 15313, 9683, 15309, 9712, 15298, 9759, 15277, 9797, 15229, 9773, 15166, 9668, 15084, 9487, 14995, 9274, 14898, 8910, 14800, 8539, 14697, 8234, 14590, 7790, 14479, 7409, 14367, 7067, 14178, 6621, 15337, 8619, 15337, 8631, 15333, 8677, 15325, 8769, 15305, 8871, 15264, 8940, 15202, 8909, 15119, 8775, 15022, 8565, 14916, 8328, 14804, 8009, 14688, 7614, 14569, 7287, 14448, 6888, 14321, 6483, 14088, 6171, 15350, 7402, 15350, 7419, 15347, 7480, 15340, 7613, 15322, 7804, 15287, 7973, 15229, 8057, 15148, 8012, 15046, 7846, 14933, 7611, 14810, 7357, 14682, 7069, 14552, 6656, 14421, 6316, 14251, 5948, 14007, 5528, 15356, 5942, 15356, 5977, 15353, 6119, 15348, 6294, 15332, 6551, 15302, 6824, 15249, 7044, 15171, 7122, 15070, 7050, 14949, 6861, 14818, 6611, 14679, 6349, 14538, 6067, 14398, 5651, 14189, 5311, 13935, 4958, 15359, 4123, 15359, 4153, 15356, 4296, 15353, 4646, 15338, 5160, 15311, 5508, 15263, 5829, 15188, 6042, 15088, 6094, 14966, 6001, 14826, 5796, 14678, 5543, 14527, 5287, 14377, 4985, 14133, 4586, 13869, 4257, 15360, 1563, 15360, 1642, 15358, 2076, 15354, 2636, 15341, 3350, 15317, 4019, 15273, 4429, 15203, 4732, 15105, 4911, 14981, 4932, 14836, 4818, 14679, 4621, 14517, 4386, 14359, 4156, 14083, 3795, 13808, 3437, 15360, 122, 15360, 137, 15358, 285, 15355, 636, 15344, 1274, 15322, 2177, 15281, 2765, 15215, 3223, 15120, 3451, 14995, 3569, 14846, 3567, 14681, 3466, 14511, 3305, 14344, 3121, 14037, 2800, 13753, 2467, 15360, 0, 15360, 1, 15359, 21, 15355, 89, 15346, 253, 15325, 479, 15287, 796, 15225, 1148, 15133, 1492, 15008, 1749, 14856, 1882, 14685, 1886, 14506, 1783, 14324, 1608, 13996, 1398, 13702, 1183]);
let Ei = null;
function Ax() {
  return Ei === null && (Ei = new Yl(wx, 16, 16, Ur, Ui), Ei.name = "DFG_LUT", Ei.minFilter = Pt, Ei.magFilter = Pt, Ei.wrapS = Ci, Ei.wrapT = Ci, Ei.generateMipmaps = !1, Ei.needsUpdate = !0), Ei;
}
class Rx {
  constructor(e = {}) {
    const { canvas: t = qp(), context: i = null, depth: n = !0, stencil: s = !1, alpha: a = !1, antialias: o = !1, premultipliedAlpha: l = !0, preserveDrawingBuffer: c = !1, powerPreference: h = "default", failIfMajorPerformanceCaveat: d = !1, reversedDepthBuffer: u = !1, outputBufferType: p = $t } = e;
    this.isWebGLRenderer = !0;
    let f;
    if (i !== null) {
      if (typeof WebGLRenderingContext < "u" && i instanceof WebGLRenderingContext) throw new Error("THREE.WebGLRenderer: WebGL 1 is not supported since r163.");
      f = i.getContextAttributes().alpha;
    } else f = a;
    const v = p, m = /* @__PURE__ */ new Set([kl, Hl, Vl]), g = /* @__PURE__ */ new Set([$t, Ni, rs, ns, Ol, Fl]), E = new Uint32Array(4), R = new Int32Array(4), y = new P();
    let S = null, T = null;
    const A = [], x = [];
    let b = null;
    this.domElement = t, this.debug = { checkShaderErrors: !0, diagnostics: { keywords: !1 }, onShaderError: null }, this.autoClear = !0, this.autoClearColor = !0, this.autoClearDepth = !0, this.autoClearStencil = !0, this.sortObjects = !0, this.clippingPlanes = [], this.localClippingEnabled = !1, this.toneMapping = Ii, this.toneMappingExposure = 1, this.transmissionResolutionScale = 1;
    const N = this;
    let O = !1, k = null, q = null, B = null, G = null;
    this._outputColorSpace = Ot;
    let Z = 0, X = 0, te = null, V = -1, K = null;
    const ee = new gt(), Ue = new gt();
    let ue = null;
    const Ye = new be(0);
    let ke = 0, je = t.width, Y = t.height, $ = 1, le = null, Oe = null;
    const xe = new gt(0, 0, je, Y), Te = new gt(0, 0, je, Y);
    let He = !1;
    const Ke = new Kl();
    let Ve = !1, vt = !1;
    const Mt = new Fe(), dt = new P(), Je = new gt(), At = { background: null, fog: null, environment: null, overrideMaterial: null, isScene: !0 };
    let tt = !1;
    function _t() {
      return te === null ? $ : 1;
    }
    let C = i;
    function xt(M, I) {
      return t.getContext(M, I);
    }
    let Qe, w, _, F, W, j, oe, he, D, se, ce, Me, ie, ge, Se, Pe, Ie, U, Q, re, _e, ae, J;
    try {
      const M = { alpha: !0, depth: n, stencil: s, antialias: o, premultipliedAlpha: l, preserveDrawingBuffer: c, powerPreference: h, failIfMajorPerformanceCaveat: d };
      if ("setAttribute" in t && t.setAttribute("data-engine", `three.js r${Ir}`), t.addEventListener("webglcontextlost", Le, !1), t.addEventListener("webglcontextrestored", Ne, !1), t.addEventListener("webglcontextcreationerror", Ut, !1), C === null) {
        const I = "webgl2";
        if (C = xt(I, M), C === null) throw xt(I) ? new Error("THREE.WebGLRenderer: Error creating WebGL context with your selected attributes.") : new Error("THREE.WebGLRenderer: Error creating WebGL context.");
      }
      me();
    } catch (M) {
      throw t.removeEventListener("webglcontextlost", Le, !1), t.removeEventListener("webglcontextrestored", Ne, !1), t.removeEventListener("webglcontextcreationerror", Ut, !1), Be("WebGLRenderer: " + M.message), M;
    }
    function me() {
      Qe = new Av(C), Qe.init(), _e = new vx(C, Qe), w = new _v(C, Qe, e, _e), _ = new gx(C, Qe), w.reversedDepthBuffer && u && _.buffers.depth.setReversed(!0), q = C.createFramebuffer(), B = C.createFramebuffer(), G = C.createFramebuffer(), F = new Cv(C), W = new ix(), j = new _x(C, Qe, _, W, w, _e, F), oe = new wv(N), he = new Im(C), ae = new mv(C, he), D = new Rv(C, he, F, ae), se = new Iv(C, D, he, ae, F), U = new Lv(C, w, j), Se = new vv(W), ce = new tx(N, oe, Qe, w, ae, Se), Me = new Ex(N, W), ie = new nx(), ge = new hx(Qe), Ie = new fv(N, oe, _, se, f, l), Pe = new mx(N, se, w), J = new bx(C, F, w, _), Q = new gv(C, Qe, F), re = new Pv(C, Qe, F), F.programs = ce.programs, N.capabilities = w, N.extensions = Qe, N.properties = W, N.renderLists = ie, N.shadowMap = Pe, N.state = _, N.info = F;
    }
    v !== $t && (b = new Uv(v, t.width, t.height, o, n, s));
    const pe = new Sx(N, C);
    this.xr = pe, this.getContext = function() {
      return C;
    }, this.getContextAttributes = function() {
      return C.getContextAttributes();
    }, this.forceContextLoss = function() {
      const M = Qe.get("WEBGL_lose_context");
      M && M.loseContext();
    }, this.forceContextRestore = function() {
      const M = Qe.get("WEBGL_lose_context");
      M && M.restoreContext();
    }, this.getPixelRatio = function() {
      return $;
    }, this.setPixelRatio = function(M) {
      M !== void 0 && ($ = M, this.setSize(je, Y, !1));
    }, this.getSize = function(M) {
      return M.set(je, Y);
    }, this.setSize = function(M, I, z = !0) {
      if (pe.isPresenting) {
        Re("WebGLRenderer: Can't change size while VR device is presenting.");
        return;
      }
      je = M, Y = I, t.width = Math.floor(M * $), t.height = Math.floor(I * $), z === !0 && (t.style.width = M + "px", t.style.height = I + "px"), b !== null && b.setSize(t.width, t.height), this.setViewport(0, 0, M, I);
    }, this.getDrawingBufferSize = function(M) {
      return M.set(je * $, Y * $).floor();
    }, this.setDrawingBufferSize = function(M, I, z) {
      je = M, Y = I, $ = z, t.width = Math.floor(M * z), t.height = Math.floor(I * z), this.setViewport(0, 0, M, I);
    }, this.setEffects = function(M) {
      if (v === $t) {
        Be("WebGLRenderer: setEffects() requires outputBufferType set to HalfFloatType or FloatType.");
        return;
      }
      if (M) {
        for (let I = 0; I < M.length; I++) if (M[I].isOutputPass === !0) {
          Re("WebGLRenderer: OutputPass is not needed in setEffects(). Tone mapping and color space conversion are applied automatically.");
          break;
        }
      }
      b.setEffects(M || []);
    }, this.getCurrentViewport = function(M) {
      return M.copy(ee);
    }, this.getViewport = function(M) {
      return M.copy(xe);
    }, this.setViewport = function(M, I, z, L) {
      M.isVector4 ? xe.set(M.x, M.y, M.z, M.w) : xe.set(M, I, z, L), _.viewport(ee.copy(xe).multiplyScalar($).round());
    }, this.getScissor = function(M) {
      return M.copy(Te);
    }, this.setScissor = function(M, I, z, L) {
      M.isVector4 ? Te.set(M.x, M.y, M.z, M.w) : Te.set(M, I, z, L), _.scissor(Ue.copy(Te).multiplyScalar($).round());
    }, this.getScissorTest = function() {
      return He;
    }, this.setScissorTest = function(M) {
      _.setScissorTest(He = M);
    }, this.setOpaqueSort = function(M) {
      le = M;
    }, this.setTransparentSort = function(M) {
      Oe = M;
    }, this.getClearColor = function(M) {
      return M.copy(Ie.getClearColor());
    }, this.setClearColor = function() {
      Ie.setClearColor(...arguments);
    }, this.getClearAlpha = function() {
      return Ie.getClearAlpha();
    }, this.setClearAlpha = function() {
      Ie.setClearAlpha(...arguments);
    }, this.clear = function(M = !0, I = !0, z = !0) {
      let L = 0;
      if (M) {
        let H = !1;
        if (te !== null) {
          const ne = te.texture.format;
          H = m.has(ne);
        }
        if (H) {
          const ne = te.texture.type, de = g.has(ne), ve = Ie.getClearColor(), ye = Ie.getClearAlpha(), we = ve.r, ze = ve.g, et = ve.b;
          de ? (E[0] = we, E[1] = ze, E[2] = et, E[3] = ye, C.clearBufferuiv(C.COLOR, 0, E)) : (R[0] = we, R[1] = ze, R[2] = et, R[3] = ye, C.clearBufferiv(C.COLOR, 0, R));
        } else L |= C.COLOR_BUFFER_BIT;
      }
      I && (L |= C.DEPTH_BUFFER_BIT, this.state.buffers.depth.setMask(!0)), z && (L |= C.STENCIL_BUFFER_BIT, this.state.buffers.stencil.setMask(4294967295)), L !== 0 && C.clear(L);
    }, this.clearColor = function() {
      this.clear(!0, !1, !1);
    }, this.clearDepth = function() {
      this.clear(!1, !0, !1);
    }, this.clearStencil = function() {
      this.clear(!1, !1, !0);
    }, this.setNodesHandler = function(M) {
      M.setRenderer(this), k = M;
    }, this.dispose = function() {
      t.removeEventListener("webglcontextlost", Le, !1), t.removeEventListener("webglcontextrestored", Ne, !1), t.removeEventListener("webglcontextcreationerror", Ut, !1), Ie.dispose(), ie.dispose(), ge.dispose(), W.dispose(), oe.dispose(), se.dispose(), ae.dispose(), J.dispose(), ce.dispose(), pe.dispose(), pe.removeEventListener("sessionstart", gr), pe.removeEventListener("sessionend", _r), ci.stop();
    };
    function Le(M) {
      M.preventDefault(), ha("WebGLRenderer: Context Lost."), O = !0;
    }
    function Ne() {
      ha("WebGLRenderer: Context Restored."), O = !1;
      const M = F.autoReset, I = Pe.enabled, z = Pe.autoUpdate, L = Pe.needsUpdate, H = Pe.type;
      me(), F.autoReset = M, Pe.enabled = I, Pe.autoUpdate = z, Pe.needsUpdate = L, Pe.type = H;
    }
    function Ut(M) {
      Be("WebGLRenderer: A WebGL context could not be created. Reason: ", M.statusMessage);
    }
    function Xt(M) {
      const I = M.target;
      I.removeEventListener("dispose", Xt), Xe(I);
    }
    function Xe(M) {
      Mi(M), W.remove(M);
    }
    function Mi(M) {
      const I = W.get(M).programs;
      I !== void 0 && (I.forEach(function(z) {
        ce.releaseProgram(z);
      }), M.isShaderMaterial && ce.releaseShaderCache(M));
    }
    this.renderBufferDirect = function(M, I, z, L, H, ne) {
      I === null && (I = At);
      const de = H.isMesh && H.matrixWorld.determinantAffine() < 0, ve = Ra(M, I, z, L, H);
      _.setMaterial(L, de);
      let ye = z.index, we = 1;
      if (L.wireframe === !0) {
        if (ye = D.getWireframeAttribute(z), ye === void 0) return;
        we = 2;
      }
      const ze = z.drawRange, et = z.attributes.position;
      let Ae = ze.start * we, ct = (ze.start + ze.count) * we;
      ne !== null && (Ae = Math.max(Ae, ne.start * we), ct = Math.min(ct, (ne.start + ne.count) * we)), ye !== null ? (Ae = Math.max(Ae, 0), ct = Math.min(ct, ye.count)) : et != null && (Ae = Math.max(Ae, 0), ct = Math.min(ct, et.count));
      const bt = ct - Ae;
      if (bt < 0 || bt === 1 / 0) return;
      ae.setup(H, L, ve, z, ye);
      let pt, ft = Q;
      if (ye !== null && (pt = he.get(ye), ft = re, ft.setIndex(pt)), H.isMesh) L.wireframe === !0 ? (_.setLineWidth(L.wireframeLinewidth * _t()), ft.setMode(C.LINES)) : ft.setMode(C.TRIANGLES);
      else if (H.isLine) {
        let St = L.linewidth;
        St === void 0 && (St = 1), _.setLineWidth(St * _t()), H.isLineSegments ? ft.setMode(C.LINES) : H.isLineLoop ? ft.setMode(C.LINE_LOOP) : ft.setMode(C.LINE_STRIP);
      } else H.isPoints ? ft.setMode(C.POINTS) : H.isSprite && ft.setMode(C.TRIANGLES);
      if (H.isBatchedMesh) if (Qe.get("WEBGL_multi_draw")) ft.renderMultiDraw(H._multiDrawStarts, H._multiDrawCounts, H._multiDrawCount);
      else {
        const St = H._multiDrawStarts, Ee = H._multiDrawCounts, Vt = H._multiDrawCount, Mr = ye ? he.get(ye).bytesPerElement : 1, ii = W.get(L).currentProgram.getUniforms();
        for (let Si = 0; Si < Vt; Si++) ii.setValue(C, "_gl_DrawID", Si), ft.render(St[Si] / Mr, Ee[Si]);
      }
      else if (H.isInstancedMesh) ft.renderInstances(Ae, bt, H.count);
      else if (z.isInstancedBufferGeometry) {
        const St = z._maxInstanceCount !== void 0 ? z._maxInstanceCount : 1 / 0, Ee = Math.min(z.instanceCount, St);
        ft.renderInstances(Ae, bt, Ee);
      } else ft.render(Ae, bt);
    };
    function jt(M, I, z, L) {
      k !== null && M.isNodeMaterial && k.setObject(L, M), Ve === !0 && Se.setState(M, z, !1), M.transparent === !0 && M.side === ai && M.forceSinglePass === !1 ? (M.side = Gt, M.needsUpdate = !0, Hi(M, I, L), M.side = fr, M.needsUpdate = !0, Hi(M, I, L), M.side = ai) : Hi(M, I, L);
    }
    this.compile = function(M, I, z = null) {
      z === null && (z = M), k !== null && k.renderStart(M, I, z), T = ge.get(z), T.init(I), x.push(T), z.traverseVisible(function(H) {
        H.isLight && H.layers.test(I.layers) && (T.pushLight(H), H.castShadow && T.pushShadow(H));
      }), M !== z && M.traverseVisible(function(H) {
        H.isLight && H.layers.test(I.layers) && (T.pushLight(H), H.castShadow && T.pushShadow(H));
      }), T.setupLights(), k !== null && k.updateLights(T.state.lightsArray), vt = this.localClippingEnabled, Ve = Se.init(this.clippingPlanes, vt), Ve === !0 && Se.setGlobalState(this.clippingPlanes, I), k !== null && Pe.render(T.state.shadowsArray, z, I);
      const L = /* @__PURE__ */ new Set();
      return M.traverse(function(H) {
        if (!(H.isMesh || H.isPoints || H.isLine || H.isSprite)) return;
        const ne = H.material;
        if (ne) if (Array.isArray(ne)) for (let de = 0; de < ne.length; de++) {
          const ve = ne[de];
          jt(ve, z, I, H), L.add(ve);
        }
        else jt(ne, z, I, H), L.add(ne);
      }), T = x.pop(), k !== null && k.renderEnd(), L;
    }, this.compileAsync = function(M, I, z = null) {
      const L = this.compile(M, I, z);
      return new Promise((H) => {
        function ne() {
          if (L.forEach(function(de) {
            const ve = W.get(de).currentProgram;
            (ve === void 0 || ve.isReady()) && L.delete(de);
          }), L.size === 0) {
            H(M);
            return;
          }
          setTimeout(ne, 10);
        }
        Qe.get("KHR_parallel_shader_compile") !== null ? ne() : setTimeout(ne, 10);
      });
    };
    let qt = null;
    function ti(M) {
      qt && qt(M);
    }
    function gr() {
      ci.stop();
    }
    function _r() {
      ci.start();
    }
    const ci = new pd();
    ci.setAnimationLoop(ti), typeof self < "u" && ci.setContext(self), this.setAnimationLoop = function(M) {
      qt = M, pe.setAnimationLoop(M), M === null ? ci.stop() : ci.start();
    }, pe.addEventListener("sessionstart", gr), pe.addEventListener("sessionend", _r), this.render = function(M, I) {
      if (I !== void 0 && I.isCamera !== !0) {
        Be("WebGLRenderer.render: camera is not an instance of THREE.Camera.");
        return;
      }
      if (O === !0) return;
      k !== null && k.renderStart(M, I);
      const z = pe.enabled === !0 && pe.isPresenting === !0, L = b !== null && (te === null || z) && b.begin(N, te);
      if (M.matrixWorldAutoUpdate === !0 && M.updateMatrixWorld(), I.parent === null && I.matrixWorldAutoUpdate === !0 && I.updateMatrixWorld(), pe.enabled === !0 && pe.isPresenting === !0 && (b === null || b.isCompositing() === !1) && (pe.cameraAutoUpdate === !0 && pe.updateCamera(I), I = pe.getCamera()), M.isScene === !0 && M.onBeforeRender(N, M, I, te), T = ge.get(M, x.length), T.init(I), T.state.textureUnits = j.getTextureUnits(), x.push(T), Mt.multiplyMatrices(I.projectionMatrix, I.matrixWorldInverse), Ke.setFromProjectionMatrix(Mt, Li, I.reversedDepth), vt = this.localClippingEnabled, Ve = Se.init(this.clippingPlanes, vt), S = ie.get(M, A.length), S.init(), A.push(S), pe.enabled === !0 && pe.isPresenting === !0) {
        const ne = N.xr.getDepthSensingMesh();
        ne !== null && vr(ne, I, -1 / 0, N.sortObjects);
      }
      vr(M, I, 0, N.sortObjects), S.finish(), k !== null && k.updateLights(T.state.lightsArray), N.sortObjects === !0 && S.sort(le, Oe), tt = pe.enabled === !1 || pe.isPresenting === !1 || pe.hasDepthSensing() === !1, tt && Ie.addToRenderList(S, M), this.info.render.frame++, this.info.autoReset === !0 && this.info.reset(), Ve === !0 && Se.beginShadows();
      const H = T.state.shadowsArray;
      if (Pe.render(H, M, I), Ve === !0 && Se.endShadows(), (L && b.hasRenderPass()) === !1) {
        const ne = S.opaque, de = S.transmissive;
        if (T.setupLights(), I.isArrayCamera) {
          const ve = I.cameras;
          if (de.length > 0) for (let ye = 0, we = ve.length; ye < we; ye++) {
            const ze = ve[ye];
            Bi(ne, de, M, ze);
          }
          tt && Ie.render(M);
          for (let ye = 0, we = ve.length; ye < we; ye++) {
            const ze = ve[ye];
            Fi(S, M, ze, ze.viewport);
          }
        } else de.length > 0 && Bi(ne, de, M, I), tt && Ie.render(M), Fi(S, M, I);
      }
      te !== null && X === 0 && (j.updateMultisampleRenderTarget(te), j.updateRenderTargetMipmap(te)), L && b.end(N), M.isScene === !0 && M.onAfterRender(N, M, I), ae.resetDefaultState(), V = -1, K = null, x.pop(), x.length > 0 ? (T = x[x.length - 1], j.setTextureUnits(T.state.textureUnits), Ve === !0 && Se.setGlobalState(N.clippingPlanes, T.state.camera)) : T = null, A.pop(), A.length > 0 ? S = A[A.length - 1] : S = null, k !== null && k.renderEnd();
    };
    function vr(M, I, z, L) {
      if (M.visible === !1) return;
      if (M.layers.test(I.layers)) {
        if (M.isGroup) z = M.renderOrder;
        else if (M.isLOD) M.autoUpdate === !0 && M.update(I);
        else if (M.isLightProbeGrid) T.pushLightProbeGrid(M);
        else if (M.isLight) T.pushLight(M), M.castShadow && T.pushShadow(M);
        else if (M.isSprite) {
          if (!M.frustumCulled || M.intersectsFrustum(Ke)) {
            L && Je.setFromMatrixPosition(M.matrixWorld).applyMatrix4(Mt);
            const ne = se.update(M), de = M.material;
            de.visible && S.push(M, ne, de, z, Je.z, null, I);
          }
        } else if ((M.isMesh || M.isLine || M.isPoints) && (!M.frustumCulled || M.intersectsFrustum(Ke))) {
          const ne = se.update(M), de = M.material;
          if (L && (M.boundingSphere !== void 0 ? (M.boundingSphere === null && M.computeBoundingSphere(), Je.copy(M.boundingSphere.center)) : (ne.boundingSphere === null && ne.computeBoundingSphere(), Je.copy(ne.boundingSphere.center)), Je.applyMatrix4(M.matrixWorld).applyMatrix4(Mt)), Array.isArray(de)) {
            const ve = ne.groups;
            for (let ye = 0, we = ve.length; ye < we; ye++) {
              const ze = ve[ye], et = de[ze.materialIndex];
              et && et.visible && S.push(M, ne, et, z, Je.z, ze, I);
            }
          } else de.visible && S.push(M, ne, de, z, Je.z, null, I);
        }
      }
      const H = M.children;
      for (let ne = 0, de = H.length; ne < de; ne++) vr(H[ne], I, z, L);
    }
    function Fi(M, I, z, L) {
      const { opaque: H, transmissive: ne, transparent: de } = M;
      T.setupLightsView(z), Ve === !0 && Se.setGlobalState(N.clippingPlanes, z), L && _.viewport(ee.copy(L)), H.length > 0 && yi(H, I, z), ne.length > 0 && yi(ne, I, z), de.length > 0 && yi(de, I, z), _.buffers.depth.setTest(!0), _.buffers.depth.setMask(!0), _.buffers.color.setMask(!0), _.setPolygonOffset(!1);
    }
    function Bi(M, I, z, L) {
      if ((z.isScene === !0 ? z.overrideMaterial : null) !== null) return;
      if (T.state.transmissionRenderTarget[L.id] === void 0) {
        const et = Qe.has("EXT_color_buffer_half_float") || Qe.has("EXT_color_buffer_float");
        T.state.transmissionRenderTarget[L.id] = new _i(1, 1, { generateMipmaps: !0, type: et ? Ui : $t, minFilter: Yi, samples: Math.max(4, w.samples), stencilBuffer: s, resolveDepthBuffer: !1, resolveStencilBuffer: !1, storeMultisampledDepthBuffer: !1, storeMultisampledStencilBuffer: !1, colorSpace: Ze.workingColorSpace });
      }
      const H = T.state.transmissionRenderTarget[L.id], ne = L.viewport || ee;
      H.setSize(ne.z * N.transmissionResolutionScale, ne.w * N.transmissionResolutionScale);
      const de = N.getRenderTarget(), ve = N.getActiveCubeFace(), ye = N.getActiveMipmapLevel();
      N.setRenderTarget(H), N.getClearColor(Ye), ke = N.getClearAlpha(), ke < 1 && N.setClearColor(16777215, 0.5), N.clear(), tt && Ie.render(z);
      const we = N.toneMapping;
      N.toneMapping = Ii;
      const ze = L.viewport;
      if (L.viewport !== void 0 && (L.viewport = void 0), T.setupLightsView(L), Ve === !0 && Se.setGlobalState(N.clippingPlanes, L), yi(M, z, L), j.updateMultisampleRenderTarget(H), j.updateRenderTargetMipmap(H), Qe.has("WEBGL_multisampled_render_to_texture") === !1) {
        let et = !1;
        for (let Ae = 0, ct = I.length; Ae < ct; Ae++) {
          const bt = I[Ae], { object: pt, geometry: ft, material: St, group: Ee } = bt;
          if (St.side === ai && pt.layers.test(L.layers)) {
            const Vt = St.side;
            St.side = Gt, St.needsUpdate = !0, Vi(pt, z, L, ft, St, Ee), St.side = Vt, St.needsUpdate = !0, et = !0;
          }
        }
        et === !0 && (j.updateMultisampleRenderTarget(H), j.updateRenderTargetMipmap(H));
      }
      N.setRenderTarget(de, ve, ye), N.setClearColor(Ye, ke), ze !== void 0 && (L.viewport = ze), N.toneMapping = we;
    }
    function yi(M, I, z) {
      const L = I.isScene === !0 ? I.overrideMaterial : null;
      for (let H = 0, ne = M.length; H < ne; H++) {
        const de = M[H], { object: ve, geometry: ye, group: we } = de;
        let ze = de.material;
        ze.allowOverride === !0 && L !== null && (ze = L), ve.layers.test(z.layers) && Vi(ve, I, z, ye, ze, we);
      }
    }
    function Vi(M, I, z, L, H, ne) {
      k !== null && H.isNodeMaterial && k.setObject(M, H), M.onBeforeRender(N, I, z, L, H, ne), M.modelViewMatrix.multiplyMatrices(z.matrixWorldInverse, M.matrixWorld), M.normalMatrix.getNormalMatrix(M.modelViewMatrix), H.onBeforeRender(N, I, z, L, M, ne), H.transparent === !0 && H.side === ai && H.forceSinglePass === !1 ? (H.side = Gt, H.needsUpdate = !0, N.renderBufferDirect(z, I, L, H, M, ne), H.side = fr, H.needsUpdate = !0, N.renderBufferDirect(z, I, L, H, M, ne), H.side = ai) : N.renderBufferDirect(z, I, L, H, M, ne), M.onAfterRender(N, I, z, L, H, ne);
    }
    function Hi(M, I, z) {
      I.isScene !== !0 && (I = At);
      const L = W.get(M), H = T.state.lights, ne = T.state.shadowsArray, de = H.state.version, ve = ce.getParameters(M, H.state, ne, I, z, T.state.lightProbeGridArray), ye = ce.getProgramCacheKey(ve);
      let we = L.programs;
      L.environment = M.isMeshStandardMaterial || M.isMeshLambertMaterial || M.isMeshPhongMaterial ? I.environment : null, L.fog = I.fog;
      const ze = M.isMeshStandardMaterial || M.isMeshLambertMaterial && !M.envMap || M.isMeshPhongMaterial && !M.envMap;
      L.envMap = oe.get(M.envMap || L.environment, ze), L.envMapRotation = L.environment !== null && M.envMap === null ? I.environmentRotation : M.envMapRotation, we === void 0 && (M.addEventListener("dispose", Xt), we = /* @__PURE__ */ new Map(), L.programs = we);
      let et = we.get(ye);
      if (et !== void 0) {
        if (L.currentProgram === et && L.lightsStateVersion === de) return fs(M, ve), et;
      } else ve.uniforms = ce.getUniforms(M), k !== null && M.isNodeMaterial && k.build(M, z, ve), M.onBeforeCompile(ve, N), et = ce.acquireProgram(ve, ye), we.set(ye, et), L.uniforms = ve.uniforms;
      const Ae = L.uniforms;
      return (!M.isShaderMaterial && !M.isRawShaderMaterial || M.clipping === !0) && (Ae.clippingPlanes = Se.uniform), fs(M, ve), L.needsLights = xr(M), L.lightsStateVersion = de, L.needsLights && (Ae.ambientLightColor.value = H.state.ambient, Ae.lightProbe.value = H.state.probe, Ae.sunLights.value = H.state.sun, Ae.sunLightShadows.value = H.state.sunShadow, Ae.directionalLights.value = H.state.directional, Ae.directionalLightShadows.value = H.state.directionalShadow, Ae.spotLights.value = H.state.spot, Ae.spotLightShadows.value = H.state.spotShadow, Ae.rectAreaLights.value = H.state.rectArea, Ae.ltc_1.value = H.state.rectAreaLTC1, Ae.ltc_2.value = H.state.rectAreaLTC2, Ae.pointLights.value = H.state.point, Ae.pointLightShadows.value = H.state.pointShadow, Ae.hemisphereLights.value = H.state.hemi, Ae.sunShadowMatrix.value = H.state.sunShadowMatrix, Ae.sunShadowCascade.value = H.state.sunShadowCascade, Ae.directionalShadowMatrix.value = H.state.directionalShadowMatrix, Ae.spotLightMatrix.value = H.state.spotLightMatrix, Ae.spotLightMap.value = H.state.spotLightMap, Ae.pointShadowMatrix.value = H.state.pointShadowMatrix), L.lightProbeGrid = T.state.lightProbeGridArray.length > 0, L.currentProgram = et, L.uniformsList = null, et;
    }
    function wn(M) {
      if (M.uniformsList === null) {
        const I = M.currentProgram.getUniforms();
        M.uniformsList = ea.seqWithValue(I.seq, M.uniforms);
      }
      return M.uniformsList;
    }
    function fs(M, I) {
      const z = W.get(M);
      z.outputColorSpace = I.outputColorSpace, z.batching = I.batching, z.batchingColor = I.batchingColor, z.instancing = I.instancing, z.instancingColor = I.instancingColor, z.instancingMorph = I.instancingMorph, z.skinning = I.skinning, z.morphTargets = I.morphTargets, z.morphNormals = I.morphNormals, z.morphColors = I.morphColors, z.morphTargetsCount = I.morphTargetsCount, z.numClippingPlanes = I.numClippingPlanes, z.numIntersection = I.numClipIntersection, z.vertexAlphas = I.vertexAlphas, z.vertexTangents = I.vertexTangents, z.toneMapping = I.toneMapping;
    }
    function Aa(M, I) {
      if (M.length === 0) return null;
      if (M.length === 1) return M[0].texture !== null ? M[0] : null;
      y.setFromMatrixPosition(I.matrixWorld);
      for (let z = 0, L = M.length; z < L; z++) {
        const H = M[z];
        if (H.texture !== null && H.boundingBox.containsPoint(y)) return H;
      }
      return null;
    }
    function Ra(M, I, z, L, H) {
      I.isScene !== !0 && (I = At), j.resetTextureUnits();
      const ne = I.fog, de = L.isMeshStandardMaterial || L.isMeshLambertMaterial || L.isMeshPhongMaterial ? I.environment : null, ve = te === null ? N.outputColorSpace : te.isXRRenderTarget === !0 ? te.texture.colorSpace : Ze.workingColorSpace, ye = L.isMeshStandardMaterial || L.isMeshLambertMaterial && !L.envMap || L.isMeshPhongMaterial && !L.envMap, we = oe.get(L.envMap || de, ye), ze = L.vertexColors === !0 && !!z.attributes.color && z.attributes.color.itemSize === 4, et = !!z.attributes.tangent && (!!L.normalMap || L.anisotropy > 0), Ae = !!z.morphAttributes.position, ct = !!z.morphAttributes.normal, bt = !!z.morphAttributes.color;
      let pt = Ii;
      L.toneMapped && (te === null || te.isXRRenderTarget === !0) && (pt = N.toneMapping);
      const ft = z.morphAttributes.position || z.morphAttributes.normal || z.morphAttributes.color, St = ft !== void 0 ? ft.length : 0, Ee = W.get(L), Vt = T.state.lights;
      if (Ve === !0 && (vt === !0 || M !== K)) {
        const ut = M === K && L.id === V;
        Se.setState(L, M, ut);
      }
      let Mr = !1;
      L.version === Ee.__version ? (Ee.needsLights && Ee.lightsStateVersion !== Vt.state.version || Ee.outputColorSpace !== ve || H.isBatchedMesh && Ee.batching === !1 || !H.isBatchedMesh && Ee.batching === !0 || H.isBatchedMesh && Ee.batchingColor === !0 && H._colorsTexture === null || H.isBatchedMesh && Ee.batchingColor === !1 && H._colorsTexture !== null || H.isInstancedMesh && Ee.instancing === !1 || !H.isInstancedMesh && Ee.instancing === !0 || H.isSkinnedMesh && Ee.skinning === !1 || !H.isSkinnedMesh && Ee.skinning === !0 || H.isInstancedMesh && Ee.instancingColor === !0 && H.instanceColor === null || H.isInstancedMesh && Ee.instancingColor === !1 && H.instanceColor !== null || H.isInstancedMesh && Ee.instancingMorph === !0 && H.morphTexture === null || H.isInstancedMesh && Ee.instancingMorph === !1 && H.morphTexture !== null || Ee.envMap !== we || L.fog === !0 && Ee.fog !== ne || Ee.numClippingPlanes !== void 0 && (Ee.numClippingPlanes !== Se.numPlanes || Ee.numIntersection !== Se.numIntersection) || Ee.vertexAlphas !== ze || Ee.vertexTangents !== et || Ee.morphTargets !== Ae || Ee.morphNormals !== ct || Ee.morphColors !== bt || Ee.toneMapping !== pt || Ee.morphTargetsCount !== St || !!Ee.lightProbeGrid != T.state.lightProbeGridArray.length > 0) && (Mr = !0) : (Mr = !0, Ee.__version = L.version);
      let ii = Ee.currentProgram;
      Mr === !0 && (ii = Hi(L, I, H), k && L.isNodeMaterial && k.onUpdateProgram(L, ii, Ee));
      let Si = !1, tr = !1, Or = !1;
      const ht = ii.getUniforms(), Tt = Ee.uniforms;
      if (_.useProgram(ii.program) && (Si = !0, tr = !0, Or = !0), L.id !== V && (V = L.id, tr = !0), Ee.needsLights) {
        const ut = Aa(T.state.lightProbeGridArray, H);
        Ee.lightProbeGrid !== ut && (Ee.lightProbeGrid = ut, tr = !0);
      }
      if (Si || K !== M) {
        _.buffers.depth.getReversed() && M.reversedDepth !== !0 && (M._reversedDepth = !0, M.updateProjectionMatrix()), ht.setValue(C, "projectionMatrix", M.projectionMatrix), ht.setValue(C, "viewMatrix", M.matrixWorldInverse);
        const ut = ht.map.cameraPosition;
        ut !== void 0 && ut.setValue(C, dt.setFromMatrixPosition(M.matrixWorld)), w.logarithmicDepthBuffer && ht.setValue(C, "logDepthBufFC", 2 / (Math.log(M.far + 1) / Math.LN2)), (L.isMeshPhongMaterial || L.isMeshToonMaterial || L.isMeshLambertMaterial || L.isMeshBasicMaterial || L.isMeshStandardMaterial || L.isShaderMaterial) && ht.setValue(C, "isOrthographic", M.isOrthographicCamera === !0), K !== M && (K = M, tr = !0, Or = !0);
      }
      if (Ee.needsLights && (Vt.state.sunShadowMap.length > 0 && ht.setValue(C, "sunShadowMap", Vt.state.sunShadowMap, j), Vt.state.directionalShadowMap.length > 0 && ht.setValue(C, "directionalShadowMap", Vt.state.directionalShadowMap, j), Vt.state.spotShadowMap.length > 0 && ht.setValue(C, "spotShadowMap", Vt.state.spotShadowMap, j), Vt.state.pointShadowMap.length > 0 && ht.setValue(C, "pointShadowMap", Vt.state.pointShadowMap, j)), H.isSkinnedMesh) {
        ht.setOptional(C, H, "bindMatrix"), ht.setOptional(C, H, "bindMatrixInverse");
        const ut = H.skeleton;
        ut && (ut.boneTexture === null && ut.computeBoneTexture(), ht.setValue(C, "boneTexture", ut.boneTexture, j));
      }
      H.isBatchedMesh && (ht.setOptional(C, H, "batchingTexture"), ht.setValue(C, "batchingTexture", H._matricesTexture, j), ht.setOptional(C, H, "batchingIdTexture"), ht.setValue(C, "batchingIdTexture", H._indirectTexture, j), ht.setOptional(C, H, "batchingColorTexture"), H._colorsTexture !== null && ht.setValue(C, "batchingColorTexture", H._colorsTexture, j));
      const ir = z.morphAttributes;
      if ((ir.position !== void 0 || ir.normal !== void 0 || ir.color !== void 0) && U.update(H, z, ii), (tr || Ee.receiveShadow !== H.receiveShadow) && (Ee.receiveShadow = H.receiveShadow, ht.setValue(C, "receiveShadow", H.receiveShadow)), (L.isMeshStandardMaterial || L.isMeshLambertMaterial || L.isMeshPhongMaterial) && L.envMap === null && I.environment !== null && (Tt.envMapIntensity.value = I.environmentIntensity), Tt.dfgLUT !== void 0 && (Tt.dfgLUT.value = Ax()), tr) {
        if (ht.setValue(C, "toneMappingExposure", N.toneMappingExposure), Ee.needsLights && ms(Tt, Or), ne && L.fog === !0 && Me.refreshFogUniforms(Tt, ne), Me.refreshMaterialUniforms(Tt, L, $, Y, T.state.transmissionRenderTarget[M.id]), Ee.needsLights && Ee.lightProbeGrid) {
          const ut = Ee.lightProbeGrid;
          Tt.probesSH.value = ut.texture, Tt.probesMin.value.copy(ut.boundingBox.min), Tt.probesMax.value.copy(ut.boundingBox.max), Tt.probesResolution.value.copy(ut.resolution);
        }
        ea.upload(C, wn(Ee), Tt, j);
      }
      if (L.isShaderMaterial && L.uniformsNeedUpdate === !0 && (ea.upload(C, wn(Ee), Tt, j), L.uniformsNeedUpdate = !1), L.isSpriteMaterial && ht.setValue(C, "center", H.center), ht.setValue(C, "modelViewMatrix", H.modelViewMatrix), ht.setValue(C, "normalMatrix", H.normalMatrix), ht.setValue(C, "modelMatrix", H.matrixWorld), L.uniformsGroups !== void 0) {
        const ut = L.uniformsGroups;
        for (let Rn = 0, Fr = ut.length; Rn < Fr; Rn++) {
          const ic = ut[Rn];
          J.update(ic, ii), J.bind(ic, ii);
        }
      }
      return ii;
    }
    function ms(M, I) {
      M.ambientLightColor.needsUpdate = I, M.lightProbe.needsUpdate = I, M.sunLights.needsUpdate = I, M.sunLightShadows.needsUpdate = I, M.directionalLights.needsUpdate = I, M.directionalLightShadows.needsUpdate = I, M.pointLights.needsUpdate = I, M.pointLightShadows.needsUpdate = I, M.spotLights.needsUpdate = I, M.spotLightShadows.needsUpdate = I, M.rectAreaLights.needsUpdate = I, M.hemisphereLights.needsUpdate = I;
    }
    function xr(M) {
      return M.isMeshLambertMaterial || M.isMeshToonMaterial || M.isMeshPhongMaterial || M.isMeshStandardMaterial || M.isShadowMaterial || M.isShaderMaterial && M.lights === !0;
    }
    this.getActiveCubeFace = function() {
      return Z;
    }, this.getActiveMipmapLevel = function() {
      return X;
    }, this.getRenderTarget = function() {
      return te;
    }, this.setRenderTargetTextures = function(M, I, z) {
      const L = W.get(M);
      L.__autoAllocateDepthBuffer = M.resolveDepthBuffer === !1, L.__autoAllocateDepthBuffer === !1 && (L.__useRenderToTexture = !1), W.get(M.texture).__webglTexture = I, W.get(M.depthTexture).__webglTexture = L.__autoAllocateDepthBuffer ? void 0 : z, L.__hasExternalTextures = !0;
    }, this.setRenderTargetFramebuffer = function(M, I) {
      const z = W.get(M);
      z.__webglFramebuffer = I, z.__useDefaultFramebuffer = I === void 0;
    }, this.setRenderTarget = function(M, I = 0, z = 0) {
      te = M, Z = I, X = z;
      let L = null, H = !1, ne = !1;
      if (M) {
        const de = W.get(M);
        if (de.__useDefaultFramebuffer !== void 0) {
          _.bindFramebuffer(C.FRAMEBUFFER, de.__webglFramebuffer), ee.copy(M.viewport), Ue.copy(M.scissor), ue = M.scissorTest, _.viewport(ee), _.scissor(Ue), _.setScissorTest(ue), V = -1;
          return;
        } else if (de.__webglFramebuffer === void 0) j.setupRenderTarget(M);
        else if (de.__hasExternalTextures) j.rebindTextures(M, W.get(M.texture).__webglTexture, W.get(M.depthTexture).__webglTexture);
        else if (M.depthBuffer) {
          const we = M.depthTexture;
          if (de.__boundDepthTexture !== we) {
            if (we !== null && W.has(we) && (M.width !== we.image.width || M.height !== we.image.height)) throw new Error("THREE.WebGLRenderer: Attached DepthTexture is initialized to the incorrect size.");
            j.setupDepthRenderbuffer(M);
          }
        }
        const ve = M.texture;
        (ve.isData3DTexture || ve.isDataArrayTexture || ve.isCompressedArrayTexture) && (ne = !0);
        const ye = W.get(M).__webglFramebuffer;
        M.isWebGLCubeRenderTarget ? (Array.isArray(ye[I]) ? L = ye[I][z] : L = ye[I], H = !0) : M.samples > 0 && j.useMultisampledRTT(M) === !1 ? L = W.get(M).__webglMultisampledFramebuffer : Array.isArray(ye) ? L = ye[z] : L = ye, ee.copy(M.viewport), Ue.copy(M.scissor), ue = M.scissorTest;
      } else ee.copy(xe).multiplyScalar($).floor(), Ue.copy(Te).multiplyScalar($).floor(), ue = He;
      if (z !== 0 && (L = q), _.bindFramebuffer(C.FRAMEBUFFER, L) && _.drawBuffers(M, L), _.viewport(ee), _.scissor(Ue), _.setScissorTest(ue), H) {
        const de = W.get(M.texture);
        C.framebufferTexture2D(C.FRAMEBUFFER, C.COLOR_ATTACHMENT0, C.TEXTURE_CUBE_MAP_POSITIVE_X + I, de.__webglTexture, z);
      } else if (ne) {
        const de = I;
        for (let ve = 0; ve < M.textures.length; ve++) {
          const ye = W.get(M.textures[ve]);
          C.framebufferTextureLayer(C.FRAMEBUFFER, C.COLOR_ATTACHMENT0 + ve, ye.__webglTexture, z, de);
        }
      } else if (M !== null && z !== 0) {
        const de = W.get(M.texture);
        C.framebufferTexture2D(C.FRAMEBUFFER, C.COLOR_ATTACHMENT0, C.TEXTURE_2D, de.__webglTexture, z);
      }
      V = -1;
    };
    function An(M) {
      const I = W.get(M);
      return (I.__readFormat !== M.format || I.__readType !== M.type) && (I.__readFormat = M.format, I.__readType = M.type, I.__formatReadable = w.textureFormatReadable(M.format), I.__typeReadable = w.textureTypeReadable(M.type)), I;
    }
    this.readRenderTargetPixels = function(M, I, z, L, H, ne, de, ve = 0) {
      if (!(M && M.isWebGLRenderTarget)) {
        Be("WebGLRenderer.readRenderTargetPixels: renderTarget is not THREE.WebGLRenderTarget.");
        return;
      }
      let ye = W.get(M).__webglFramebuffer;
      if (M.isWebGLCubeRenderTarget && de !== void 0 && (ye = ye[de]), ye) {
        _.bindFramebuffer(C.FRAMEBUFFER, ye);
        try {
          const we = M.textures[ve], ze = we.format, et = we.type;
          M.textures.length > 1 && C.readBuffer(C.COLOR_ATTACHMENT0 + ve);
          const Ae = An(we);
          if (Ae.__formatReadable === !1) {
            Be("WebGLRenderer.readRenderTargetPixels: renderTarget is not in RGBA or implementation defined format.");
            return;
          }
          if (Ae.__typeReadable === !1) {
            Be("WebGLRenderer.readRenderTargetPixels: renderTarget is not in UnsignedByteType or implementation defined type.");
            return;
          }
          I >= 0 && I <= M.width - L && z >= 0 && z <= M.height - H && C.readPixels(I, z, L, H, _e.convert(ze), _e.convert(et), ne);
        } finally {
          const we = te !== null ? W.get(te).__webglFramebuffer : null;
          _.bindFramebuffer(C.FRAMEBUFFER, we);
        }
      }
    }, this.readRenderTargetPixelsAsync = async function(M, I, z, L, H, ne, de, ve = 0) {
      if (!(M && M.isWebGLRenderTarget)) throw new Error("THREE.WebGLRenderer.readRenderTargetPixels: renderTarget is not THREE.WebGLRenderTarget.");
      let ye = W.get(M).__webglFramebuffer;
      if (M.isWebGLCubeRenderTarget && de !== void 0 && (ye = ye[de]), ye) if (I >= 0 && I <= M.width - L && z >= 0 && z <= M.height - H) {
        _.bindFramebuffer(C.FRAMEBUFFER, ye);
        const we = M.textures[ve], ze = we.format, et = we.type;
        M.textures.length > 1 && C.readBuffer(C.COLOR_ATTACHMENT0 + ve);
        const Ae = An(we);
        if (Ae.__formatReadable === !1) throw new Error("THREE.WebGLRenderer.readRenderTargetPixelsAsync: renderTarget is not in RGBA or implementation defined format.");
        if (Ae.__typeReadable === !1) throw new Error("THREE.WebGLRenderer.readRenderTargetPixelsAsync: renderTarget is not in UnsignedByteType or implementation defined type.");
        const ct = C.createBuffer();
        C.bindBuffer(C.PIXEL_PACK_BUFFER, ct), C.bufferData(C.PIXEL_PACK_BUFFER, ne.byteLength, C.STREAM_READ), C.readPixels(I, z, L, H, _e.convert(ze), _e.convert(et), 0), C.bindBuffer(C.PIXEL_PACK_BUFFER, null);
        const bt = te !== null ? W.get(te).__webglFramebuffer : null;
        _.bindFramebuffer(C.FRAMEBUFFER, bt);
        const pt = C.fenceSync(C.SYNC_GPU_COMMANDS_COMPLETE, 0);
        return C.flush(), await Yp(C, pt, 4), C.bindBuffer(C.PIXEL_PACK_BUFFER, ct), C.getBufferSubData(C.PIXEL_PACK_BUFFER, 0, ne), C.bindBuffer(C.PIXEL_PACK_BUFFER, null), C.deleteBuffer(ct), C.deleteSync(pt), ne;
      } else throw new Error("THREE.WebGLRenderer.readRenderTargetPixelsAsync: requested read bounds are out of range.");
    }, this.copyFramebufferToTexture = function(M, I = null, z = 0) {
      const L = Math.pow(2, -z), H = Math.floor(M.image.width * L), ne = Math.floor(M.image.height * L), de = I !== null ? I.x : 0, ve = I !== null ? I.y : 0;
      j.setTexture2D(M, 0), C.copyTexSubImage2D(C.TEXTURE_2D, z, 0, 0, de, ve, H, ne), _.unbindTexture();
    }, this.copyTextureToTexture = function(M, I, z = null, L = null, H = 0, ne = 0) {
      let de, ve, ye, we, ze, et, Ae, ct, bt;
      const pt = M.isCompressedTexture ? M.mipmaps[ne] : M.image;
      if (z !== null) de = z.max.x - z.min.x, ve = z.max.y - z.min.y, ye = z.isBox3 ? z.max.z - z.min.z : 1, we = z.min.x, ze = z.min.y, et = z.isBox3 ? z.min.z : 0;
      else {
        const Tt = Math.pow(2, -H);
        de = Math.floor(pt.width * Tt), ve = Math.floor(pt.height * Tt), M.isDataArrayTexture ? ye = pt.depth : M.isData3DTexture ? ye = Math.floor(pt.depth * Tt) : ye = 1, we = 0, ze = 0, et = 0;
      }
      L !== null ? (Ae = L.x, ct = L.y, bt = L.z) : (Ae = 0, ct = 0, bt = 0);
      const ft = _e.convert(I.format), St = _e.convert(I.type);
      let Ee;
      I.isData3DTexture ? (j.setTexture3D(I, 0), Ee = C.TEXTURE_3D) : I.isDataArrayTexture || I.isCompressedArrayTexture ? (j.setTexture2DArray(I, 0), Ee = C.TEXTURE_2D_ARRAY) : (j.setTexture2D(I, 0), Ee = C.TEXTURE_2D), _.activeTexture(C.TEXTURE0), _.pixelStorei(C.UNPACK_FLIP_Y_WEBGL, I.flipY), _.pixelStorei(C.UNPACK_PREMULTIPLY_ALPHA_WEBGL, I.premultiplyAlpha), _.pixelStorei(C.UNPACK_ALIGNMENT, I.unpackAlignment);
      const Vt = _.getParameter(C.UNPACK_ROW_LENGTH), Mr = _.getParameter(C.UNPACK_IMAGE_HEIGHT), ii = _.getParameter(C.UNPACK_SKIP_PIXELS), Si = _.getParameter(C.UNPACK_SKIP_ROWS), tr = _.getParameter(C.UNPACK_SKIP_IMAGES);
      _.pixelStorei(C.UNPACK_ROW_LENGTH, pt.width), _.pixelStorei(C.UNPACK_IMAGE_HEIGHT, pt.height), _.pixelStorei(C.UNPACK_SKIP_PIXELS, we), _.pixelStorei(C.UNPACK_SKIP_ROWS, ze), _.pixelStorei(C.UNPACK_SKIP_IMAGES, et);
      const Or = M.isDataArrayTexture || M.isData3DTexture, ht = I.isDataArrayTexture || I.isData3DTexture;
      if (M.isDepthTexture) {
        const Tt = W.get(M), ir = W.get(I), ut = W.get(Tt.__renderTarget), Rn = W.get(ir.__renderTarget);
        _.bindFramebuffer(C.READ_FRAMEBUFFER, ut.__webglFramebuffer), _.bindFramebuffer(C.DRAW_FRAMEBUFFER, Rn.__webglFramebuffer);
        for (let Fr = 0; Fr < ye; Fr++) Or && (C.framebufferTextureLayer(C.READ_FRAMEBUFFER, C.COLOR_ATTACHMENT0, W.get(M).__webglTexture, H, et + Fr), C.framebufferTextureLayer(C.DRAW_FRAMEBUFFER, C.COLOR_ATTACHMENT0, W.get(I).__webglTexture, ne, bt + Fr)), C.blitFramebuffer(we, ze, de, ve, Ae, ct, de, ve, C.DEPTH_BUFFER_BIT, C.NEAREST);
        _.bindFramebuffer(C.READ_FRAMEBUFFER, null), _.bindFramebuffer(C.DRAW_FRAMEBUFFER, null);
      } else if (H !== 0 || M.isRenderTargetTexture || W.has(M)) {
        const Tt = W.get(M), ir = W.get(I);
        _.bindFramebuffer(C.READ_FRAMEBUFFER, B), _.bindFramebuffer(C.DRAW_FRAMEBUFFER, G);
        for (let ut = 0; ut < ye; ut++) Or ? C.framebufferTextureLayer(C.READ_FRAMEBUFFER, C.COLOR_ATTACHMENT0, Tt.__webglTexture, H, et + ut) : C.framebufferTexture2D(C.READ_FRAMEBUFFER, C.COLOR_ATTACHMENT0, C.TEXTURE_2D, Tt.__webglTexture, H), ht ? C.framebufferTextureLayer(C.DRAW_FRAMEBUFFER, C.COLOR_ATTACHMENT0, ir.__webglTexture, ne, bt + ut) : C.framebufferTexture2D(C.DRAW_FRAMEBUFFER, C.COLOR_ATTACHMENT0, C.TEXTURE_2D, ir.__webglTexture, ne), H !== 0 ? C.blitFramebuffer(we, ze, de, ve, Ae, ct, de, ve, C.COLOR_BUFFER_BIT, C.NEAREST) : ht ? C.copyTexSubImage3D(Ee, ne, Ae, ct, bt + ut, we, ze, de, ve) : C.copyTexSubImage2D(Ee, ne, Ae, ct, we, ze, de, ve);
        _.bindFramebuffer(C.READ_FRAMEBUFFER, null), _.bindFramebuffer(C.DRAW_FRAMEBUFFER, null);
      } else ht ? M.isDataTexture || M.isData3DTexture ? C.texSubImage3D(Ee, ne, Ae, ct, bt, de, ve, ye, ft, St, pt.data) : I.isCompressedArrayTexture ? C.compressedTexSubImage3D(Ee, ne, Ae, ct, bt, de, ve, ye, ft, pt.data) : C.texSubImage3D(Ee, ne, Ae, ct, bt, de, ve, ye, ft, St, pt) : M.isDataTexture ? C.texSubImage2D(C.TEXTURE_2D, ne, Ae, ct, de, ve, ft, St, pt.data) : M.isCompressedTexture ? C.compressedTexSubImage2D(C.TEXTURE_2D, ne, Ae, ct, pt.width, pt.height, ft, pt.data) : C.texSubImage2D(C.TEXTURE_2D, ne, Ae, ct, de, ve, ft, St, pt);
      _.pixelStorei(C.UNPACK_ROW_LENGTH, Vt), _.pixelStorei(C.UNPACK_IMAGE_HEIGHT, Mr), _.pixelStorei(C.UNPACK_SKIP_PIXELS, ii), _.pixelStorei(C.UNPACK_SKIP_ROWS, Si), _.pixelStorei(C.UNPACK_SKIP_IMAGES, tr), ne === 0 && I.generateMipmaps && C.generateMipmap(Ee), _.unbindTexture();
    }, this.initRenderTarget = function(M) {
      W.get(M).__webglFramebuffer === void 0 && j.setupRenderTarget(M);
    }, this.initTexture = function(M) {
      M.isCubeTexture ? j.setTextureCube(M, 0) : M.isData3DTexture ? j.setTexture3D(M, 0) : M.isDataArrayTexture || M.isCompressedArrayTexture ? j.setTexture2DArray(M, 0) : j.setTexture2D(M, 0), _.unbindTexture();
    }, this.resetState = function() {
      Z = 0, X = 0, te = null, _.reset(), ae.reset();
    }, typeof __THREE_DEVTOOLS__ < "u" && __THREE_DEVTOOLS__.dispatchEvent(new CustomEvent("observe", { detail: this }));
  }
  get coordinateSystem() {
    return Li;
  }
  get outputColorSpace() {
    return this._outputColorSpace;
  }
  set outputColorSpace(e) {
    this._outputColorSpace = e;
    const t = this.getContext();
    t.drawingBufferColorSpace = Ze._getDrawingBufferColorSpace(e), t.unpackColorSpace = Ze._getUnpackColorSpace();
  }
}
function Sh(r, e) {
  if (e === Up) return console.warn("THREE.BufferGeometryUtils.toTrianglesDrawMode(): Geometry already defined as triangles."), r;
  if (e === xl || e === Vu) {
    let t = r.getIndex();
    if (t === null) {
      const s = [], a = r.getAttribute("position");
      if (a !== void 0) {
        for (let o = 0; o < a.count; o++) s.push(o);
        r.setIndex(s), t = r.getIndex();
      } else return console.error("THREE.BufferGeometryUtils.toTrianglesDrawMode(): Undefined position attribute. Processing not possible."), r;
    }
    const i = t.count - 2, n = [];
    if (e === xl) for (let s = 1; s <= i; s++) n.push(t.getX(0)), n.push(t.getX(s)), n.push(t.getX(s + 1));
    else for (let s = 0; s < i; s++) s % 2 === 0 ? (n.push(t.getX(s)), n.push(t.getX(s + 1)), n.push(t.getX(s + 2))) : (n.push(t.getX(s + 2)), n.push(t.getX(s + 1)), n.push(t.getX(s)));
    return n.length / 3 !== i && console.error("THREE.BufferGeometryUtils.toTrianglesDrawMode(): Unable to generate correct amount of triangles."), r.setIndex(n), r.clearGroups(), r;
  } else return console.error("THREE.BufferGeometryUtils.toTrianglesDrawMode(): Unknown draw mode:", e), r;
}
function Px(r) {
  const e = /* @__PURE__ */ new Map(), t = /* @__PURE__ */ new Map(), i = r.clone();
  return yd(r, i, function(n, s) {
    e.set(s, n), t.set(n, s);
  }), i.traverse(function(n) {
    if (!n.isSkinnedMesh) return;
    const s = n, a = e.get(n), o = a.skeleton.bones;
    s.skeleton = a.skeleton.clone(), s.bindMatrix.copy(a.bindMatrix), s.skeleton.bones = o.map(function(l) {
      return t.get(l);
    }), s.bind(s.skeleton, s.bindMatrix);
  }), i;
}
function yd(r, e, t) {
  t(r, e);
  for (let i = 0; i < r.children.length; i++) yd(r.children[i], e.children[i], t);
}
class Sd extends En {
  constructor(e) {
    super(e), this.dracoLoader = null, this.ktx2Loader = null, this.meshoptDecoder = null, this.pluginCallbacks = [], this.register(function(t) {
      return new Ux(t);
    }), this.register(function(t) {
      return new Dx(t);
    }), this.register(function(t) {
      return new Wx(t);
    }), this.register(function(t) {
      return new Xx(t);
    }), this.register(function(t) {
      return new jx(t);
    }), this.register(function(t) {
      return new Fx(t);
    }), this.register(function(t) {
      return new Bx(t);
    }), this.register(function(t) {
      return new Vx(t);
    }), this.register(function(t) {
      return new Hx(t);
    }), this.register(function(t) {
      return new Nx(t);
    }), this.register(function(t) {
      return new kx(t);
    }), this.register(function(t) {
      return new Ox(t);
    }), this.register(function(t) {
      return new Gx(t);
    }), this.register(function(t) {
      return new zx(t);
    }), this.register(function(t) {
      return new Lx(t);
    }), this.register(function(t) {
      return new Th(t, qe.EXT_MESHOPT_COMPRESSION);
    }), this.register(function(t) {
      return new Th(t, qe.KHR_MESHOPT_COMPRESSION);
    }), this.register(function(t) {
      return new qx(t);
    });
  }
  load(e, t, i, n) {
    const s = this;
    let a;
    if (this.resourcePath !== "") a = this.resourcePath;
    else if (this.path !== "") {
      const c = ts.extractUrlBase(e);
      a = ts.resolveURL(c, this.path);
    } else a = ts.extractUrlBase(e);
    this.manager.itemStart(e);
    const o = function(c) {
      n ? n(c) : console.error(c), s.manager.itemError(e), s.manager.itemEnd(e);
    }, l = new hd(this.manager);
    l.setPath(this.path), l.setResponseType("arraybuffer"), l.setRequestHeader(this.requestHeader), l.setWithCredentials(this.withCredentials), l.load(e, function(c) {
      try {
        s.parse(c, a, function(h) {
          t(h), s.manager.itemEnd(e);
        }, o);
      } catch (h) {
        o(h);
      }
    }, i, o);
  }
  setDRACOLoader(e) {
    return this.dracoLoader = e, this;
  }
  setKTX2Loader(e) {
    return this.ktx2Loader = e, this;
  }
  setMeshoptDecoder(e) {
    return this.meshoptDecoder = e, this;
  }
  register(e) {
    return this.pluginCallbacks.indexOf(e) === -1 && this.pluginCallbacks.push(e), this;
  }
  unregister(e) {
    return this.pluginCallbacks.indexOf(e) !== -1 && this.pluginCallbacks.splice(this.pluginCallbacks.indexOf(e), 1), this;
  }
  parse(e, t, i, n) {
    let s;
    const a = {}, o = {}, l = new TextDecoder();
    if (typeof e == "string") s = JSON.parse(e);
    else if (e instanceof ArrayBuffer) if (l.decode(new Uint8Array(e, 0, 4)) === Td) {
      try {
        a[qe.KHR_BINARY_GLTF] = new Yx(e);
      } catch (h) {
        n && n(h);
        return;
      }
      s = JSON.parse(a[qe.KHR_BINARY_GLTF].content);
    } else s = JSON.parse(l.decode(e));
    else s = e;
    if (s.asset === void 0 || s.asset.version[0] < 2) {
      n && n(new Error("THREE.GLTFLoader: Unsupported asset. glTF versions >=2.0 are supported."));
      return;
    }
    const c = new oM(s, { path: t || this.resourcePath || "", crossOrigin: this.crossOrigin, requestHeader: this.requestHeader, manager: this.manager, ktx2Loader: this.ktx2Loader, meshoptDecoder: this.meshoptDecoder });
    c.fileLoader.setRequestHeader(this.requestHeader);
    for (let h = 0; h < this.pluginCallbacks.length; h++) {
      const d = this.pluginCallbacks[h](c);
      d.name || console.error("THREE.GLTFLoader: Invalid plugin found: missing name"), o[d.name] = d, a[d.name] = !0;
    }
    if (s.extensionsUsed) for (let h = 0; h < s.extensionsUsed.length; ++h) {
      const d = s.extensionsUsed[h], u = s.extensionsRequired || [];
      switch (d) {
        case qe.KHR_MATERIALS_UNLIT:
          a[d] = new Ix();
          break;
        case qe.KHR_DRACO_MESH_COMPRESSION:
          a[d] = new Kx(s, this.dracoLoader);
          break;
        case qe.KHR_TEXTURE_TRANSFORM:
          a[d] = new Zx();
          break;
        case qe.KHR_MESH_QUANTIZATION:
          a[d] = new $x();
          break;
        default:
          u.indexOf(d) >= 0 && o[d] === void 0 && console.warn('THREE.GLTFLoader: Unknown extension "' + d + '".');
      }
    }
    c.setExtensions(a), c.setPlugins(o), c.parse(i, n);
  }
  parseAsync(e, t) {
    const i = this;
    return new Promise(function(n, s) {
      i.parse(e, t, n, s);
    });
  }
}
function Cx() {
  let r = {};
  return { get: function(e) {
    return r[e];
  }, add: function(e, t) {
    r[e] = t;
  }, remove: function(e) {
    delete r[e];
  }, removeAll: function() {
    r = {};
  } };
}
function Et(r, e, t) {
  const i = r.json.materials[e];
  return i.extensions && i.extensions[t] ? i.extensions[t] : null;
}
const qe = { KHR_BINARY_GLTF: "KHR_binary_glTF", KHR_DRACO_MESH_COMPRESSION: "KHR_draco_mesh_compression", KHR_LIGHTS_PUNCTUAL: "KHR_lights_punctual", KHR_MATERIALS_CLEARCOAT: "KHR_materials_clearcoat", KHR_MATERIALS_DISPERSION: "KHR_materials_dispersion", KHR_MATERIALS_IOR: "KHR_materials_ior", KHR_MATERIALS_SHEEN: "KHR_materials_sheen", KHR_MATERIALS_SPECULAR: "KHR_materials_specular", KHR_MATERIALS_TRANSMISSION: "KHR_materials_transmission", KHR_MATERIALS_IRIDESCENCE: "KHR_materials_iridescence", KHR_MATERIALS_ANISOTROPY: "KHR_materials_anisotropy", KHR_MATERIALS_UNLIT: "KHR_materials_unlit", KHR_MATERIALS_VOLUME: "KHR_materials_volume", KHR_TEXTURE_BASISU: "KHR_texture_basisu", KHR_TEXTURE_TRANSFORM: "KHR_texture_transform", KHR_MESH_QUANTIZATION: "KHR_mesh_quantization", KHR_MATERIALS_EMISSIVE_STRENGTH: "KHR_materials_emissive_strength", EXT_MATERIALS_BUMP: "EXT_materials_bump", EXT_TEXTURE_WEBP: "EXT_texture_webp", EXT_TEXTURE_AVIF: "EXT_texture_avif", EXT_MESHOPT_COMPRESSION: "EXT_meshopt_compression", KHR_MESHOPT_COMPRESSION: "KHR_meshopt_compression", EXT_MESH_GPU_INSTANCING: "EXT_mesh_gpu_instancing" };
class Lx {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_LIGHTS_PUNCTUAL, this.cache = { refs: {}, uses: {} };
  }
  _markDefs() {
    const e = this.parser, t = this.parser.json.nodes || [];
    for (let i = 0, n = t.length; i < n; i++) {
      const s = t[i];
      s.extensions && s.extensions[this.name] && s.extensions[this.name].light !== void 0 && e._addNodeRef(this.cache, s.extensions[this.name].light);
    }
  }
  _loadLight(e) {
    const t = this.parser, i = "light:" + e;
    let n = t.cache.get(i);
    if (n) return n;
    const s = t.json, a = ((s.extensions && s.extensions[this.name] || {}).lights || [])[e];
    let o;
    const l = new be(16777215);
    a.color !== void 0 && l.setRGB(a.color[0], a.color[1], a.color[2], ei);
    const c = a.range !== void 0 ? a.range : 0;
    switch (a.type) {
      case "directional":
        o = new yl(l), o.target.position.set(0, 0, -1), o.add(o.target);
        break;
      case "point":
        o = new hm(l), o.distance = c;
        break;
      case "spot":
        o = new lm(l), o.distance = c, a.spot = a.spot || {}, a.spot.innerConeAngle = a.spot.innerConeAngle !== void 0 ? a.spot.innerConeAngle : 0, a.spot.outerConeAngle = a.spot.outerConeAngle !== void 0 ? a.spot.outerConeAngle : Math.PI / 4, o.angle = a.spot.outerConeAngle, o.penumbra = 1 - a.spot.innerConeAngle / a.spot.outerConeAngle, o.target.position.set(0, 0, -1), o.add(o.target);
        break;
      default:
        throw new Error("THREE.GLTFLoader: Unexpected light type: " + a.type);
    }
    return o.position.set(0, 0, 0), Ri(o, a), a.intensity !== void 0 && (o.intensity = a.intensity), o.name = t.createUniqueName(a.name || "light_" + e), n = Promise.resolve(o), t.cache.add(i, n), n;
  }
  getDependency(e, t) {
    if (e === "light") return this._loadLight(t);
  }
  createNodeAttachment(e) {
    const t = this, i = this.parser, n = i.json.nodes[e], s = (n.extensions && n.extensions[this.name] || {}).light;
    return s === void 0 ? null : this._loadLight(s).then(function(a) {
      return i._getNodeRef(t.cache, s, a);
    });
  }
}
class Ix {
  constructor() {
    this.name = qe.KHR_MATERIALS_UNLIT;
  }
  getMaterialType() {
    return Ki;
  }
  extendParams(e, t, i) {
    const n = [];
    e.color = new be(1, 1, 1), e.opacity = 1;
    const s = t.pbrMetallicRoughness;
    if (s) {
      if (Array.isArray(s.baseColorFactor)) {
        const a = s.baseColorFactor;
        e.color.setRGB(a[0], a[1], a[2], ei), e.opacity = a[3];
      }
      s.baseColorTexture !== void 0 && n.push(i.assignTexture(e, "map", s.baseColorTexture, Ot));
    }
    return Promise.all(n);
  }
}
class Nx {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_MATERIALS_EMISSIVE_STRENGTH;
  }
  extendMaterialParams(e, t) {
    const i = Et(this.parser, e, this.name);
    return i === null || i.emissiveStrength !== void 0 && (t.emissiveIntensity = i.emissiveStrength), Promise.resolve();
  }
}
class Ux {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_MATERIALS_CLEARCOAT;
  }
  getMaterialType(e) {
    return Et(this.parser, e, this.name) !== null ? Oi : null;
  }
  extendMaterialParams(e, t) {
    const i = Et(this.parser, e, this.name);
    if (i === null) return Promise.resolve();
    const n = [];
    if (i.clearcoatFactor !== void 0 && (t.clearcoat = i.clearcoatFactor), i.clearcoatTexture !== void 0 && n.push(this.parser.assignTexture(t, "clearcoatMap", i.clearcoatTexture)), i.clearcoatRoughnessFactor !== void 0 && (t.clearcoatRoughness = i.clearcoatRoughnessFactor), i.clearcoatRoughnessTexture !== void 0 && n.push(this.parser.assignTexture(t, "clearcoatRoughnessMap", i.clearcoatRoughnessTexture)), i.clearcoatNormalTexture !== void 0 && (n.push(this.parser.assignTexture(t, "clearcoatNormalMap", i.clearcoatNormalTexture)), i.clearcoatNormalTexture.scale !== void 0)) {
      const s = i.clearcoatNormalTexture.scale;
      t.clearcoatNormalScale = new We(s, s);
    }
    return Promise.all(n);
  }
}
class Dx {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_MATERIALS_DISPERSION;
  }
  getMaterialType(e) {
    return Et(this.parser, e, this.name) !== null ? Oi : null;
  }
  extendMaterialParams(e, t) {
    const i = Et(this.parser, e, this.name);
    return i === null || (t.dispersion = i.dispersion !== void 0 ? i.dispersion : 0), Promise.resolve();
  }
}
class Ox {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_MATERIALS_IRIDESCENCE;
  }
  getMaterialType(e) {
    return Et(this.parser, e, this.name) !== null ? Oi : null;
  }
  extendMaterialParams(e, t) {
    const i = Et(this.parser, e, this.name);
    if (i === null) return Promise.resolve();
    const n = [];
    return i.iridescenceFactor !== void 0 && (t.iridescence = i.iridescenceFactor), i.iridescenceTexture !== void 0 && n.push(this.parser.assignTexture(t, "iridescenceMap", i.iridescenceTexture)), i.iridescenceIor !== void 0 && (t.iridescenceIOR = i.iridescenceIor), t.iridescenceThicknessRange === void 0 && (t.iridescenceThicknessRange = [100, 400]), i.iridescenceThicknessMinimum !== void 0 && (t.iridescenceThicknessRange[0] = i.iridescenceThicknessMinimum), i.iridescenceThicknessMaximum !== void 0 && (t.iridescenceThicknessRange[1] = i.iridescenceThicknessMaximum), i.iridescenceThicknessTexture !== void 0 && n.push(this.parser.assignTexture(t, "iridescenceThicknessMap", i.iridescenceThicknessTexture)), Promise.all(n);
  }
}
class Fx {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_MATERIALS_SHEEN;
  }
  getMaterialType(e) {
    return Et(this.parser, e, this.name) !== null ? Oi : null;
  }
  extendMaterialParams(e, t) {
    const i = Et(this.parser, e, this.name);
    if (i === null) return Promise.resolve();
    const n = [];
    if (t.sheenColor = new be(0, 0, 0), t.sheenRoughness = 0, t.sheen = 1, i.sheenColorFactor !== void 0) {
      const s = i.sheenColorFactor;
      t.sheenColor.setRGB(s[0], s[1], s[2], ei);
    }
    return i.sheenRoughnessFactor !== void 0 && (t.sheenRoughness = i.sheenRoughnessFactor), i.sheenColorTexture !== void 0 && n.push(this.parser.assignTexture(t, "sheenColorMap", i.sheenColorTexture, Ot)), i.sheenRoughnessTexture !== void 0 && n.push(this.parser.assignTexture(t, "sheenRoughnessMap", i.sheenRoughnessTexture)), Promise.all(n);
  }
}
class Bx {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_MATERIALS_TRANSMISSION;
  }
  getMaterialType(e) {
    return Et(this.parser, e, this.name) !== null ? Oi : null;
  }
  extendMaterialParams(e, t) {
    const i = Et(this.parser, e, this.name);
    if (i === null) return Promise.resolve();
    const n = [];
    return i.transmissionFactor !== void 0 && (t.transmission = i.transmissionFactor), i.transmissionTexture !== void 0 && n.push(this.parser.assignTexture(t, "transmissionMap", i.transmissionTexture)), Promise.all(n);
  }
}
class Vx {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_MATERIALS_VOLUME;
  }
  getMaterialType(e) {
    return Et(this.parser, e, this.name) !== null ? Oi : null;
  }
  extendMaterialParams(e, t) {
    const i = Et(this.parser, e, this.name);
    if (i === null) return Promise.resolve();
    const n = [];
    t.thickness = i.thicknessFactor !== void 0 ? i.thicknessFactor : 0, i.thicknessTexture !== void 0 && n.push(this.parser.assignTexture(t, "thicknessMap", i.thicknessTexture)), t.attenuationDistance = i.attenuationDistance || 1 / 0;
    const s = i.attenuationColor || [1, 1, 1];
    return t.attenuationColor = new be().setRGB(s[0], s[1], s[2], ei), Promise.all(n);
  }
}
class Hx {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_MATERIALS_IOR;
  }
  getMaterialType(e) {
    return Et(this.parser, e, this.name) !== null ? Oi : null;
  }
  extendMaterialParams(e, t) {
    const i = Et(this.parser, e, this.name);
    return i === null || (t.ior = i.ior !== void 0 ? i.ior : 1.5, t.ior === 0 && (t.ior = 1e3)), Promise.resolve();
  }
}
class kx {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_MATERIALS_SPECULAR;
  }
  getMaterialType(e) {
    return Et(this.parser, e, this.name) !== null ? Oi : null;
  }
  extendMaterialParams(e, t) {
    const i = Et(this.parser, e, this.name);
    if (i === null) return Promise.resolve();
    const n = [];
    t.specularIntensity = i.specularFactor !== void 0 ? i.specularFactor : 1, i.specularTexture !== void 0 && n.push(this.parser.assignTexture(t, "specularIntensityMap", i.specularTexture));
    const s = i.specularColorFactor || [1, 1, 1];
    return t.specularColor = new be().setRGB(s[0], s[1], s[2], ei), i.specularColorTexture !== void 0 && n.push(this.parser.assignTexture(t, "specularColorMap", i.specularColorTexture, Ot)), Promise.all(n);
  }
}
class zx {
  constructor(e) {
    this.parser = e, this.name = qe.EXT_MATERIALS_BUMP;
  }
  getMaterialType(e) {
    return Et(this.parser, e, this.name) !== null ? Oi : null;
  }
  extendMaterialParams(e, t) {
    const i = Et(this.parser, e, this.name);
    if (i === null) return Promise.resolve();
    const n = [];
    return t.bumpScale = i.bumpFactor !== void 0 ? i.bumpFactor : 1, i.bumpTexture !== void 0 && n.push(this.parser.assignTexture(t, "bumpMap", i.bumpTexture)), Promise.all(n);
  }
}
class Gx {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_MATERIALS_ANISOTROPY;
  }
  getMaterialType(e) {
    return Et(this.parser, e, this.name) !== null ? Oi : null;
  }
  extendMaterialParams(e, t) {
    const i = Et(this.parser, e, this.name);
    if (i === null) return Promise.resolve();
    const n = [];
    return i.anisotropyStrength !== void 0 && (t.anisotropy = i.anisotropyStrength), i.anisotropyRotation !== void 0 && (t.anisotropyRotation = i.anisotropyRotation), i.anisotropyTexture !== void 0 && n.push(this.parser.assignTexture(t, "anisotropyMap", i.anisotropyTexture)), Promise.all(n);
  }
}
class Wx {
  constructor(e) {
    this.parser = e, this.name = qe.KHR_TEXTURE_BASISU;
  }
  loadTexture(e) {
    const t = this.parser, i = t.json, n = i.textures[e];
    if (!n.extensions || !n.extensions[this.name]) return null;
    const s = n.extensions[this.name], a = t.options.ktx2Loader;
    if (!a) {
      if (i.extensionsRequired && i.extensionsRequired.indexOf(this.name) >= 0) throw new Error("THREE.GLTFLoader: setKTX2Loader must be called before loading KTX2 textures");
      return null;
    }
    return t.loadTextureImage(e, s.source, a);
  }
}
class Xx {
  constructor(e) {
    this.parser = e, this.name = qe.EXT_TEXTURE_WEBP;
  }
  loadTexture(e) {
    const t = this.name, i = this.parser, n = i.json, s = n.textures[e];
    if (!s.extensions || !s.extensions[t]) return null;
    const a = s.extensions[t], o = n.images[a.source];
    let l = i.textureLoader;
    if (o.uri) {
      const c = i.options.manager.getHandler(o.uri);
      c !== null && (l = c);
    }
    return i.loadTextureImage(e, a.source, l);
  }
}
class jx {
  constructor(e) {
    this.parser = e, this.name = qe.EXT_TEXTURE_AVIF;
  }
  loadTexture(e) {
    const t = this.name, i = this.parser, n = i.json, s = n.textures[e];
    if (!s.extensions || !s.extensions[t]) return null;
    const a = s.extensions[t], o = n.images[a.source];
    let l = i.textureLoader;
    if (o.uri) {
      const c = i.options.manager.getHandler(o.uri);
      c !== null && (l = c);
    }
    return i.loadTextureImage(e, a.source, l);
  }
}
class Th {
  constructor(e, t) {
    this.name = t, this.parser = e;
  }
  loadBufferView(e) {
    const t = this.parser.json, i = t.bufferViews[e];
    if (i.extensions && i.extensions[this.name]) {
      const n = i.extensions[this.name], s = this.parser.getDependency("buffer", n.buffer), a = this.parser.options.meshoptDecoder;
      if (!a || !a.supported) {
        if (t.extensionsRequired && t.extensionsRequired.indexOf(this.name) >= 0) throw new Error("THREE.GLTFLoader: setMeshoptDecoder must be called before loading compressed files");
        return null;
      }
      return s.then(function(o) {
        const l = n.byteOffset || 0, c = n.byteLength || 0, h = n.count, d = n.byteStride, u = new Uint8Array(o, l, c);
        return a.decodeGltfBufferAsync ? a.decodeGltfBufferAsync(h, d, u, n.mode, n.filter).then(function(p) {
          return p.buffer;
        }) : a.ready.then(function() {
          const p = new ArrayBuffer(h * d);
          return a.decodeGltfBuffer(new Uint8Array(p), h, d, u, n.mode, n.filter), p;
        });
      });
    } else return null;
  }
}
class qx {
  constructor(e) {
    this.name = qe.EXT_MESH_GPU_INSTANCING, this.parser = e;
  }
  createNodeMesh(e) {
    const t = this.parser.json, i = t.nodes[e];
    if (!i.extensions || !i.extensions[this.name] || i.mesh === void 0) return null;
    const n = t.meshes[i.mesh];
    for (const l of n.primitives) if (l.mode !== si.TRIANGLES && l.mode !== si.TRIANGLE_STRIP && l.mode !== si.TRIANGLE_FAN && l.mode !== void 0) return null;
    const s = i.extensions[this.name].attributes, a = [], o = {};
    for (const l in s) a.push(this.parser.getDependency("accessor", s[l]).then((c) => (o[l] = c, o[l])));
    return a.length < 1 ? null : (a.push(this.parser.createNodeMesh(e)), Promise.all(a).then((l) => {
      const c = l.pop(), h = c.isGroup ? c.children : [c], d = l[0].count, u = [];
      for (const p of h) {
        const f = new Fe(), v = new P(), m = new Ce(), g = new P(1, 1, 1), E = new Ff(p.geometry, p.material, d);
        for (let y = 0; y < d; y++) o.TRANSLATION && v.fromBufferAttribute(o.TRANSLATION, y), o.ROTATION && m.fromBufferAttribute(o.ROTATION, y), o.SCALE && g.fromBufferAttribute(o.SCALE, y), E.setMatrixAt(y, f.compose(v, m, g));
        let R = null;
        for (const y in o) if (y === "_COLOR_0") {
          const S = o[y];
          E.instanceColor = new ua(S.array, S.itemSize, S.normalized);
        } else if (y !== "TRANSLATION" && y !== "ROTATION" && y !== "SCALE") {
          if (R === null) {
            const T = E.geometry;
            R = new yt(), R.name = T.name;
            for (const A in T.attributes) R.setAttribute(A, T.attributes[A]);
            for (const A in T.morphAttributes) R.morphAttributes[A] = T.morphAttributes[A];
            T.index !== null && R.setIndex(T.index), R.morphTargetsRelative = T.morphTargetsRelative;
            for (const A of T.groups) R.addGroup(A.start, A.count, A.materialIndex);
            T.boundingBox !== null && (R.boundingBox = T.boundingBox.clone()), T.boundingSphere !== null && (R.boundingSphere = T.boundingSphere.clone()), R.drawRange.start = T.drawRange.start, R.drawRange.count = T.drawRange.count, R.userData = Object.assign({}, T.userData), E.geometry = R;
          }
          const S = o[y];
          R.setAttribute(y, new ua(S.array, S.itemSize, S.normalized));
        }
        at.prototype.copy.call(E, p), this.parser.assignFinalMaterial(E), u.push(E);
      }
      return c.isGroup ? (c.clear(), c.add(...u), c) : u[0];
    }));
  }
}
const Td = "glTF", Vn = 12, Eh = { JSON: 1313821514, BIN: 5130562 };
class Yx {
  constructor(e) {
    this.name = qe.KHR_BINARY_GLTF, this.content = null, this.body = null;
    const t = new DataView(e, 0, Vn), i = new TextDecoder();
    if (this.header = { magic: i.decode(new Uint8Array(e.slice(0, 4))), version: t.getUint32(4, !0), length: t.getUint32(8, !0) }, this.header.magic !== Td) throw new Error("THREE.GLTFLoader: Unsupported glTF-Binary header.");
    if (this.header.version < 2) throw new Error("THREE.GLTFLoader: Legacy binary file detected.");
    const n = this.header.length - Vn, s = new DataView(e, Vn);
    let a = 0;
    for (; a < n; ) {
      const o = s.getUint32(a, !0);
      a += 4;
      const l = s.getUint32(a, !0);
      if (a += 4, l === Eh.JSON) {
        const c = new Uint8Array(e, Vn + a, o);
        this.content = i.decode(c);
      } else if (l === Eh.BIN) {
        const c = Vn + a;
        this.body = e.slice(c, c + o);
      }
      a += o;
    }
    if (this.content === null) throw new Error("THREE.GLTFLoader: JSON content not found.");
  }
}
class Kx {
  constructor(e, t) {
    if (!t) throw new Error("THREE.GLTFLoader: No DRACOLoader instance provided.");
    this.name = qe.KHR_DRACO_MESH_COMPRESSION, this.json = e, this.dracoLoader = t, this.dracoLoader.preload();
  }
  decodePrimitive(e, t) {
    const i = this.json, n = this.dracoLoader, s = e.extensions[this.name].bufferView, a = e.extensions[this.name].attributes, o = {}, l = {}, c = {};
    for (const h in a) {
      const d = El[h] || h.toLowerCase();
      o[d] = a[h];
    }
    for (const h in e.attributes) {
      const d = El[h] || h.toLowerCase();
      if (a[h] !== void 0) {
        const u = i.accessors[e.attributes[h]], p = mn[u.componentType];
        c[d] = p.name, l[d] = u.normalized === !0;
      }
    }
    return t.getDependency("bufferView", s).then(function(h) {
      return new Promise(function(d, u) {
        n.decodeDracoFile(h, function(p) {
          for (const f in p.attributes) {
            const v = p.attributes[f], m = l[f];
            m !== void 0 && (v.normalized = m);
          }
          d(p);
        }, o, c, ei, u);
      });
    });
  }
}
class Zx {
  constructor() {
    this.name = qe.KHR_TEXTURE_TRANSFORM;
  }
  extendTexture(e, t) {
    if ((t.texCoord === void 0 || t.texCoord === e.channel) && t.offset === void 0 && t.rotation === void 0 && t.scale === void 0) return e;
    if (e = e.clone(), t.texCoord !== void 0 && (e.channel = t.texCoord), t.offset !== void 0 && e.offset.fromArray(t.offset), t.rotation !== void 0 && (e.rotation = t.rotation), t.scale !== void 0 && e.repeat.fromArray(t.scale), t.rotation !== void 0) {
      const i = Math.cos(e.rotation), n = Math.sin(e.rotation);
      e.matrix.set(e.repeat.x * i, e.repeat.y * n, e.offset.x, -e.repeat.x * n, e.repeat.y * i, e.offset.y, 0, 0, 1), e.matrixAutoUpdate = !1;
    }
    return e.needsUpdate = !0, e;
  }
}
class $x {
  constructor() {
    this.name = qe.KHR_MESH_QUANTIZATION;
  }
}
class Ed extends Tn {
  constructor(e, t, i, n) {
    super(e, t, i, n);
  }
  copySampleValue_(e) {
    const t = this.resultBuffer, i = this.sampleValues, n = this.valueSize, s = e * n * 3 + n;
    for (let a = 0; a !== n; a++) t[a] = i[s + a];
    return t;
  }
  interpolate_(e, t, i, n) {
    const s = this.resultBuffer, a = this.sampleValues, o = this.valueSize, l = o * 2, c = o * 3, h = n - t, d = (i - t) / h, u = d * d, p = u * d, f = e * c, v = f - c, m = -2 * p + 3 * u, g = p - u, E = 1 - m, R = g - u + d;
    for (let y = 0; y !== o; y++) {
      const S = a[v + y + o], T = a[v + y + l] * h, A = a[f + y + o], x = a[f + y] * h;
      s[y] = E * S + R * T + m * A + g * x;
    }
    return s;
  }
}
const Qx = new Ce();
class Jx extends Ed {
  interpolate_(e, t, i, n) {
    const s = super.interpolate_(e, t, i, n);
    return Qx.fromArray(s).normalize().toArray(s), s;
  }
}
const si = { POINTS: 0, LINES: 1, LINE_LOOP: 2, LINE_STRIP: 3, TRIANGLES: 4, TRIANGLE_STRIP: 5, TRIANGLE_FAN: 6 }, mn = { 5120: Int8Array, 5121: Uint8Array, 5122: Int16Array, 5123: Uint16Array, 5125: Uint32Array, 5126: Float32Array }, bh = { 9728: Rt, 9729: Pt, 9984: Iu, 9985: Ys, 9986: Yn, 9987: Yi }, wh = { 33071: Ci, 33648: na, 10497: _n }, po = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT2: 4, MAT3: 9, MAT4: 16 }, El = { POSITION: "position", NORMAL: "normal", TANGENT: "tangent", TEXCOORD_0: "uv", TEXCOORD_1: "uv1", TEXCOORD_2: "uv2", TEXCOORD_3: "uv3", COLOR_0: "color", WEIGHTS_0: "skinWeight", JOINTS_0: "skinIndex" }, cr = { scale: "scale", translation: "position", rotation: "quaternion", weights: "morphTargetInfluences" }, eM = { CUBICSPLINE: void 0, LINEAR: as, STEP: ss }, fo = { OPAQUE: "OPAQUE", MASK: "MASK", BLEND: "BLEND" };
function tM(r) {
  return r.DefaultMaterial === void 0 && (r.DefaultMaterial = new Zl({ color: 16777215, emissive: 0, metalness: 1, roughness: 1, transparent: !1, depthTest: !0, side: fr })), r.DefaultMaterial;
}
function wr(r, e, t) {
  for (const i in t.extensions) r[i] === void 0 && (e.userData.gltfExtensions = e.userData.gltfExtensions || {}, e.userData.gltfExtensions[i] = t.extensions[i]);
}
function Ri(r, e) {
  e.extras !== void 0 && (typeof e.extras == "object" ? Object.assign(r.userData, e.extras) : console.warn("THREE.GLTFLoader: Ignoring primitive type .extras, " + e.extras));
}
function iM(r, e, t) {
  let i = !1, n = !1, s = !1;
  for (let c = 0, h = e.length; c < h; c++) {
    const d = e[c];
    if (d.POSITION !== void 0 && (i = !0), d.NORMAL !== void 0 && (n = !0), d.COLOR_0 !== void 0 && (s = !0), i && n && s) break;
  }
  if (!i && !n && !s) return Promise.resolve(r);
  const a = [], o = [], l = [];
  for (let c = 0, h = e.length; c < h; c++) {
    const d = e[c];
    if (i) {
      const u = d.POSITION !== void 0 ? t.getDependency("accessor", d.POSITION) : r.attributes.position;
      a.push(u);
    }
    if (n) {
      const u = d.NORMAL !== void 0 ? t.getDependency("accessor", d.NORMAL) : r.attributes.normal;
      o.push(u);
    }
    if (s) {
      const u = d.COLOR_0 !== void 0 ? t.getDependency("accessor", d.COLOR_0) : r.attributes.color;
      l.push(u);
    }
  }
  return Promise.all([Promise.all(a), Promise.all(o), Promise.all(l)]).then(function(c) {
    const h = c[0], d = c[1], u = c[2];
    return i && (r.morphAttributes.position = h), n && (r.morphAttributes.normal = d), s && (r.morphAttributes.color = u), r.morphTargetsRelative = !0, r;
  });
}
function rM(r, e) {
  if (r.updateMorphTargets(), e.weights !== void 0) for (let t = 0, i = e.weights.length; t < i; t++) r.morphTargetInfluences[t] = e.weights[t];
  if (e.extras && Array.isArray(e.extras.targetNames)) {
    const t = e.extras.targetNames;
    if (r.morphTargetInfluences.length === t.length) {
      r.morphTargetDictionary = {};
      for (let i = 0, n = t.length; i < n; i++) r.morphTargetDictionary[t[i]] = i;
    } else console.warn("THREE.GLTFLoader: Invalid extras.targetNames length. Ignoring names.");
  }
}
function nM(r) {
  let e;
  const t = r.extensions && r.extensions[qe.KHR_DRACO_MESH_COMPRESSION];
  if (t ? e = "draco:" + t.bufferView + ":" + t.indices + ":" + mo(t.attributes) : e = r.indices + ":" + mo(r.attributes) + ":" + r.mode, r.targets !== void 0) for (let i = 0, n = r.targets.length; i < n; i++) e += ":" + mo(r.targets[i]);
  return e;
}
function mo(r) {
  let e = "";
  const t = Object.keys(r).sort();
  for (let i = 0, n = t.length; i < n; i++) e += t[i] + ":" + r[t[i]] + ";";
  return e;
}
function bl(r) {
  switch (r) {
    case Int8Array:
      return 1 / 127;
    case Uint8Array:
      return 1 / 255;
    case Int16Array:
      return 1 / 32767;
    case Uint16Array:
      return 1 / 65535;
    default:
      throw new Error("THREE.GLTFLoader: Unsupported normalized accessor component type.");
  }
}
function sM(r) {
  return r.search(/\.jpe?g($|\?)/i) > 0 || r.search(/^data\:image\/jpeg/) === 0 ? "image/jpeg" : r.search(/\.webp($|\?)/i) > 0 || r.search(/^data\:image\/webp/) === 0 ? "image/webp" : r.search(/\.ktx2($|\?)/i) > 0 || r.search(/^data\:image\/ktx2/) === 0 ? "image/ktx2" : "image/png";
}
const aM = new Fe();
class oM {
  constructor(e = {}, t = {}) {
    this.json = e, this.extensions = {}, this.plugins = {}, this.options = t, this.cache = new Cx(), this.associations = /* @__PURE__ */ new Map(), this.primitiveCache = {}, this.nodeCache = {}, this.meshCache = { refs: {}, uses: {} }, this.cameraCache = { refs: {}, uses: {} }, this.lightCache = { refs: {}, uses: {} }, this.sourceCache = {}, this.textureCache = {}, this.nodeNamesUsed = {};
    let i = !1, n = -1, s = !1, a = -1;
    if (typeof navigator < "u" && typeof navigator.userAgent < "u") {
      const o = navigator.userAgent;
      i = /^((?!chrome|android).)*safari/i.test(o) === !0;
      const l = o.match(/Version\/(\d+)/);
      n = i && l ? parseInt(l[1], 10) : -1, s = o.indexOf("Firefox") > -1, a = s ? o.match(/Firefox\/([0-9]+)\./)[1] : -1;
    }
    typeof createImageBitmap > "u" || i && n < 17 || s && a < 98 ? this.textureLoader = new am(this.options.manager) : this.textureLoader = new pm(this.options.manager), this.textureLoader.setCrossOrigin(this.options.crossOrigin), this.textureLoader.setRequestHeader(this.options.requestHeader), this.fileLoader = new hd(this.options.manager), this.fileLoader.setResponseType("arraybuffer"), this.options.crossOrigin === "use-credentials" && this.fileLoader.setWithCredentials(!0);
  }
  setExtensions(e) {
    this.extensions = e;
  }
  setPlugins(e) {
    this.plugins = e;
  }
  parse(e, t) {
    const i = this, n = this.json, s = this.extensions;
    this.cache.removeAll(), this.nodeCache = {}, this._invokeAll(function(a) {
      return a._markDefs && a._markDefs();
    }), Promise.all(this._invokeAll(function(a) {
      return a.beforeRoot && a.beforeRoot();
    })).then(function() {
      return Promise.all([i.getDependencies("scene"), i.getDependencies("animation"), i.getDependencies("camera")]);
    }).then(function(a) {
      const o = { scene: a[0][n.scene || 0], scenes: a[0], animations: a[1], cameras: a[2], asset: n.asset, parser: i, userData: {} };
      return wr(s, o, n), Ri(o, n), Promise.all(i._invokeAll(function(l) {
        return l.afterRoot && l.afterRoot(o);
      })).then(function() {
        for (const l of o.scenes) l.updateMatrixWorld();
        e(o);
      });
    }).catch(t);
  }
  _markDefs() {
    const e = this.json.nodes || [], t = this.json.skins || [], i = this.json.meshes || [];
    for (let n = 0, s = t.length; n < s; n++) {
      const a = t[n].joints;
      for (let o = 0, l = a.length; o < l; o++) e[a[o]].isBone = !0;
    }
    for (let n = 0, s = e.length; n < s; n++) {
      const a = e[n];
      a.mesh !== void 0 && (this._addNodeRef(this.meshCache, a.mesh), a.skin !== void 0 && (i[a.mesh].isSkinnedMesh = !0)), a.camera !== void 0 && this._addNodeRef(this.cameraCache, a.camera);
    }
  }
  _addNodeRef(e, t) {
    t !== void 0 && (e.refs[t] === void 0 && (e.refs[t] = e.uses[t] = 0), e.refs[t]++);
  }
  _getNodeRef(e, t, i) {
    if (e.refs[t] <= 1) return i;
    const n = i.clone(), s = (a, o) => {
      const l = this.associations.get(a);
      l != null && this.associations.set(o, l);
      for (const [c, h] of a.children.entries()) s(h, o.children[c]);
    };
    return s(i, n), n.name += "_instance_" + e.uses[t]++, n;
  }
  _invokeOne(e) {
    const t = Object.values(this.plugins);
    t.push(this);
    for (let i = 0; i < t.length; i++) {
      const n = e(t[i]);
      if (n) return n;
    }
    return null;
  }
  _invokeAll(e) {
    const t = Object.values(this.plugins);
    t.unshift(this);
    const i = [];
    for (let n = 0; n < t.length; n++) {
      const s = e(t[n]);
      s && i.push(s);
    }
    return i;
  }
  getDependency(e, t) {
    const i = e + ":" + t;
    let n = this.cache.get(i);
    if (!n) {
      switch (e) {
        case "scene":
          n = this.loadScene(t);
          break;
        case "node":
          n = this._invokeOne(function(s) {
            return s.loadNode && s.loadNode(t);
          });
          break;
        case "mesh":
          n = this._invokeOne(function(s) {
            return s.loadMesh && s.loadMesh(t);
          });
          break;
        case "accessor":
          n = this.loadAccessor(t);
          break;
        case "bufferView":
          n = this._invokeOne(function(s) {
            return s.loadBufferView && s.loadBufferView(t);
          });
          break;
        case "buffer":
          n = this.loadBuffer(t);
          break;
        case "material":
          n = this._invokeOne(function(s) {
            return s.loadMaterial && s.loadMaterial(t);
          });
          break;
        case "texture":
          n = this._invokeOne(function(s) {
            return s.loadTexture && s.loadTexture(t);
          });
          break;
        case "skin":
          n = this.loadSkin(t);
          break;
        case "animation":
          n = this._invokeOne(function(s) {
            return s.loadAnimation && s.loadAnimation(t);
          });
          break;
        case "camera":
          n = this.loadCamera(t);
          break;
        default:
          if (n = this._invokeOne(function(s) {
            return s != this && s.getDependency && s.getDependency(e, t);
          }), !n) throw new Error("Unknown type: " + e);
          break;
      }
      this.cache.add(i, n);
    }
    return n;
  }
  getDependencies(e) {
    let t = this.cache.get(e);
    if (!t) {
      const i = this, n = this.json[e + (e === "mesh" ? "es" : "s")] || [];
      t = Promise.all(n.map(function(s, a) {
        return i.getDependency(e, a);
      })), this.cache.add(e, t);
    }
    return t;
  }
  loadBuffer(e) {
    const t = this.json.buffers[e], i = this.fileLoader;
    if (t.type && t.type !== "arraybuffer") throw new Error("THREE.GLTFLoader: " + t.type + " buffer type is not supported.");
    if (t.uri === void 0 && e === 0) return Promise.resolve(this.extensions[qe.KHR_BINARY_GLTF].body);
    const n = this.options;
    return new Promise(function(s, a) {
      i.load(ts.resolveURL(t.uri, n.path), s, void 0, function() {
        a(new Error('THREE.GLTFLoader: Failed to load buffer "' + t.uri + '".'));
      });
    });
  }
  loadBufferView(e) {
    const t = this.json.bufferViews[e];
    return this.getDependency("buffer", t.buffer).then(function(i) {
      const n = t.byteLength || 0, s = t.byteOffset || 0;
      return i.slice(s, s + n);
    });
  }
  loadAccessor(e) {
    const t = this, i = this.json, n = this.json.accessors[e];
    if (n.bufferView === void 0 && n.sparse === void 0) {
      const a = po[n.type], o = mn[n.componentType], l = n.normalized === !0, c = new o(n.count * a);
      return Promise.resolve(new nt(c, a, l));
    }
    const s = [];
    return n.bufferView !== void 0 ? s.push(this.getDependency("bufferView", n.bufferView)) : s.push(null), n.sparse !== void 0 && (s.push(this.getDependency("bufferView", n.sparse.indices.bufferView)), s.push(this.getDependency("bufferView", n.sparse.values.bufferView))), Promise.all(s).then(function(a) {
      const o = a[0], l = po[n.type], c = mn[n.componentType], h = c.BYTES_PER_ELEMENT, d = h * l, u = n.byteOffset || 0, p = n.bufferView !== void 0 ? i.bufferViews[n.bufferView].byteStride : void 0, f = n.normalized === !0;
      let v, m;
      if (p && p !== d) {
        const g = Math.floor(u / p), E = "InterleavedBuffer:" + n.bufferView + ":" + n.componentType + ":" + g + ":" + n.count;
        let R = t.cache.get(E);
        R || (v = new c(o, g * p, n.count * p / h), R = new ql(v, p / h), t.cache.add(E, R)), m = new us(R, l, u % p / h, f);
      } else o === null ? v = new c(n.count * l) : v = new c(o, u, n.count * l), m = new nt(v, l, f);
      if (n.sparse !== void 0) {
        const g = po.SCALAR, E = mn[n.sparse.indices.componentType], R = n.sparse.indices.byteOffset || 0, y = n.sparse.values.byteOffset || 0, S = new E(a[1], R, n.sparse.count * g), T = new c(a[2], y, n.sparse.count * l);
        o !== null && (m = new nt(m.array.slice(), m.itemSize, m.normalized)), m.normalized = !1;
        for (let A = 0, x = S.length; A < x; A++) {
          const b = S[A];
          if (m.setX(b, T[A * l]), l >= 2 && m.setY(b, T[A * l + 1]), l >= 3 && m.setZ(b, T[A * l + 2]), l >= 4 && m.setW(b, T[A * l + 3]), l >= 5) throw new Error("THREE.GLTFLoader: Unsupported itemSize in sparse BufferAttribute.");
        }
        m.normalized = f;
      }
      return m;
    });
  }
  loadTexture(e) {
    const t = this.json, i = this.options, n = t.textures[e].source, s = t.images[n];
    let a = this.textureLoader;
    if (s.uri) {
      const o = i.manager.getHandler(s.uri);
      o !== null && (a = o);
    }
    return this.loadTextureImage(e, n, a);
  }
  loadTextureImage(e, t, i) {
    const n = this, s = this.json, a = s.textures[e], o = s.images[t], l = (o.uri || o.bufferView) + ":" + a.sampler;
    if (this.textureCache[l]) return this.textureCache[l];
    const c = this.loadImageSource(t, i).then(function(h) {
      h.flipY = !1, h.name = a.name || o.name || "", h.name === "" && typeof o.uri == "string" && o.uri.startsWith("data:image/") === !1 && (h.name = o.uri);
      const d = (s.samplers || {})[a.sampler] || {};
      return h.magFilter = bh[d.magFilter] || Pt, h.minFilter = bh[d.minFilter] || Yi, h.wrapS = wh[d.wrapS] || _n, h.wrapT = wh[d.wrapT] || _n, h.generateMipmaps = !h.isCompressedTexture && h.minFilter !== Rt && h.minFilter !== Pt, n.associations.set(h, { textures: e }), h;
    }).catch(function() {
      return null;
    });
    return this.textureCache[l] = c, c;
  }
  loadImageSource(e, t) {
    const i = this, n = this.json, s = this.options;
    if (this.sourceCache[e] !== void 0) return this.sourceCache[e].then((d) => d.clone());
    const a = n.images[e], o = self.URL || self.webkitURL;
    let l = a.uri || "", c = !1;
    if (a.bufferView !== void 0) l = i.getDependency("bufferView", a.bufferView).then(function(d) {
      c = !0;
      const u = new Blob([d], { type: a.mimeType });
      return l = o.createObjectURL(u), l;
    });
    else if (a.uri === void 0) throw new Error("THREE.GLTFLoader: Image " + e + " is missing URI and bufferView");
    const h = Promise.resolve(l).then(function(d) {
      return new Promise(function(u, p) {
        let f = u;
        t.isImageBitmapLoader === !0 && (f = function(v) {
          const m = new Nt(v);
          m.needsUpdate = !0, u(m);
        }), t.load(ts.resolveURL(d, s.path), f, void 0, p);
      });
    }).then(function(d) {
      return c === !0 && o.revokeObjectURL(l), Ri(d, a), d.userData.mimeType = a.mimeType || sM(a.uri), d;
    }).catch(function(d) {
      throw console.error("THREE.GLTFLoader: Couldn't load texture", l), d;
    });
    return this.sourceCache[e] = h, h;
  }
  assignTexture(e, t, i, n) {
    const s = this;
    return this.getDependency("texture", i.index).then(function(a) {
      if (!a) return null;
      if (i.texCoord !== void 0 && i.texCoord > 0 && (a = a.clone(), a.channel = i.texCoord), s.extensions[qe.KHR_TEXTURE_TRANSFORM]) {
        const o = i.extensions !== void 0 ? i.extensions[qe.KHR_TEXTURE_TRANSFORM] : void 0;
        if (o) {
          const l = s.associations.get(a);
          a = s.extensions[qe.KHR_TEXTURE_TRANSFORM].extendTexture(a, o), s.associations.set(a, l);
        }
      }
      return n !== void 0 && (a.colorSpace = n), e[t] = a, a;
    });
  }
  assignFinalMaterial(e) {
    const t = e.geometry;
    let i = e.material;
    const n = t.attributes.tangent === void 0, s = t.attributes.color !== void 0, a = t.attributes.normal === void 0;
    if (e.isPoints) {
      const o = "PointsMaterial:" + i.uuid;
      let l = this.cache.get(o);
      l || (l = new td(), vi.prototype.copy.call(l, i), l.color.copy(i.color), l.map = i.map, l.sizeAttenuation = !1, this.cache.add(o, l)), i = l;
    } else if (e.isLine) {
      const o = "LineBasicMaterial:" + i.uuid;
      let l = this.cache.get(o);
      l || (l = new Dr(), vi.prototype.copy.call(l, i), l.color.copy(i.color), l.map = i.map, this.cache.add(o, l)), i = l;
    }
    if (n || s || a) {
      let o = "ClonedMaterial:" + i.uuid + ":";
      n && (o += "derivative-tangents:"), s && (o += "vertex-colors:"), a && (o += "flat-shading:");
      let l = this.cache.get(o);
      l || (l = i.clone(), s && (l.vertexColors = !0), a && (l.flatShading = !0), n && (l.normalScale && (l.normalScale.y *= -1), l.clearcoatNormalScale && (l.clearcoatNormalScale.y *= -1)), this.cache.add(o, l), this.associations.set(l, this.associations.get(i))), i = l;
    }
    e.material = i;
  }
  getMaterialType() {
    return Zl;
  }
  loadMaterial(e) {
    const t = this, i = this.json, n = this.extensions, s = i.materials[e];
    let a;
    const o = {}, l = s.extensions || {}, c = [];
    if (l[qe.KHR_MATERIALS_UNLIT]) {
      const d = n[qe.KHR_MATERIALS_UNLIT];
      a = d.getMaterialType(), c.push(d.extendParams(o, s, t));
    } else {
      const d = s.pbrMetallicRoughness || {};
      if (o.color = new be(1, 1, 1), o.opacity = 1, Array.isArray(d.baseColorFactor)) {
        const u = d.baseColorFactor;
        o.color.setRGB(u[0], u[1], u[2], ei), o.opacity = u[3];
      }
      d.baseColorTexture !== void 0 && c.push(t.assignTexture(o, "map", d.baseColorTexture, Ot)), o.metalness = d.metallicFactor !== void 0 ? d.metallicFactor : 1, o.roughness = d.roughnessFactor !== void 0 ? d.roughnessFactor : 1, d.metallicRoughnessTexture !== void 0 && (c.push(t.assignTexture(o, "metalnessMap", d.metallicRoughnessTexture)), c.push(t.assignTexture(o, "roughnessMap", d.metallicRoughnessTexture))), a = this._invokeOne(function(u) {
        return u.getMaterialType && u.getMaterialType(e);
      }), c.push(Promise.all(this._invokeAll(function(u) {
        return u.extendMaterialParams && u.extendMaterialParams(e, o);
      })));
    }
    s.doubleSided === !0 && (o.side = ai);
    const h = s.alphaMode || fo.OPAQUE;
    if (h === fo.BLEND ? (o.transparent = !0, o.depthWrite = !1) : (o.transparent = !1, h === fo.MASK && (o.alphaTest = s.alphaCutoff !== void 0 ? s.alphaCutoff : 0.5)), s.normalTexture !== void 0 && a !== Ki && (c.push(t.assignTexture(o, "normalMap", s.normalTexture)), o.normalScale = new We(1, 1), s.normalTexture.scale !== void 0)) {
      const d = s.normalTexture.scale;
      o.normalScale.set(d, d);
    }
    if (s.occlusionTexture !== void 0 && a !== Ki && (c.push(t.assignTexture(o, "aoMap", s.occlusionTexture)), s.occlusionTexture.strength !== void 0 && (o.aoMapIntensity = s.occlusionTexture.strength)), s.emissiveFactor !== void 0 && a !== Ki) {
      const d = s.emissiveFactor;
      o.emissive = new be().setRGB(d[0], d[1], d[2], ei);
    }
    return s.emissiveTexture !== void 0 && a !== Ki && c.push(t.assignTexture(o, "emissiveMap", s.emissiveTexture, Ot)), Promise.all(c).then(function() {
      const d = new a(o);
      return s.name && (d.name = s.name), Ri(d, s), t.associations.set(d, { materials: e }), s.extensions && wr(n, d, s), d;
    });
  }
  createUniqueName(e) {
    const t = st.sanitizeNodeName(e || "");
    return t in this.nodeNamesUsed ? t + "_" + ++this.nodeNamesUsed[t] : (this.nodeNamesUsed[t] = 0, t);
  }
  loadGeometries(e) {
    const t = this, i = this.extensions, n = this.primitiveCache;
    function s(o) {
      return i[qe.KHR_DRACO_MESH_COMPRESSION].decodePrimitive(o, t).then(function(l) {
        return Ah(l, o, t);
      });
    }
    const a = [];
    for (let o = 0, l = e.length; o < l; o++) {
      const c = e[o], h = nM(c), d = n[h];
      if (d) a.push(d.promise);
      else {
        let u;
        c.extensions && c.extensions[qe.KHR_DRACO_MESH_COMPRESSION] ? u = s(c) : u = Ah(new yt(), c, t), c.mode === si.TRIANGLE_STRIP ? u = u.then((p) => Sh(p, Vu)) : c.mode === si.TRIANGLE_FAN && (u = u.then((p) => Sh(p, xl))), n[h] = { primitive: c, promise: u }, a.push(u);
      }
    }
    return Promise.all(a);
  }
  loadMesh(e) {
    const t = this, i = this.json, n = this.extensions, s = i.meshes[e], a = s.primitives, o = [];
    for (let l = 0, c = a.length; l < c; l++) {
      const h = a[l].material === void 0 ? tM(this.cache) : this.getDependency("material", a[l].material);
      o.push(h);
    }
    return o.push(t.loadGeometries(a)), Promise.all(o).then(async function(l) {
      const c = l.slice(0, l.length - 1), h = l[l.length - 1], d = [];
      for (let p = 0, f = h.length; p < f; p++) {
        const v = h[p], m = a[p];
        let g;
        const E = c[p];
        if (m.mode === si.TRIANGLES || m.mode === si.TRIANGLE_STRIP || m.mode === si.TRIANGLE_FAN || m.mode === void 0) {
          const R = s.isSkinnedMesh === !0, y = v.hasAttribute("skinIndex") && v.hasAttribute("skinWeight");
          R && y === !1 && console.warn("THREE.GLTFLoader: Missing skinIndex or skinWeight attributes. Skinning disabled."), g = R && y ? new Ju(v, E) : new Wt(v, E), g.isSkinnedMesh === !0 && g.normalizeSkinWeights();
        } else if (m.mode === si.LINES) g = new ds(v, E);
        else if (m.mode === si.LINE_STRIP) g = new ya(v, E);
        else if (m.mode === si.LINE_LOOP) g = new Vf(v, E);
        else if (m.mode === si.POINTS) g = new Hf(v, E);
        else throw new Error("THREE.GLTFLoader: Primitive mode unsupported: " + m.mode);
        Object.keys(g.geometry.morphAttributes).length > 0 && rM(g, s), g.name = t.createUniqueName(s.name || "mesh_" + e), Ri(g, s), m.extensions && wr(n, g, m), t.assignFinalMaterial(g), d.push(g);
      }
      for (let p = 0, f = d.length; p < f; p++) t.associations.set(d[p], { meshes: e, primitives: p });
      if (d.length === 1) return s.extensions && wr(n, d[0], s), d[0];
      const u = new Qt();
      s.extensions && wr(n, u, s), t.associations.set(u, { meshes: e });
      for (let p = 0, f = d.length; p < f; p++) u.add(d[p]);
      return u;
    });
  }
  loadCamera(e) {
    let t;
    const i = this.json.cameras[e], n = i[i.type];
    if (!n) {
      console.warn("THREE.GLTFLoader: Missing camera parameters.");
      return;
    }
    return i.type === "perspective" ? t = new zt(mt.radToDeg(n.yfov), n.aspectRatio || 1, n.znear || 1, n.zfar || 2e6) : i.type === "orthographic" && (t = new Ea(-n.xmag, n.xmag, n.ymag, -n.ymag, n.znear, n.zfar)), i.name && (t.name = this.createUniqueName(i.name)), Ri(t, i), Promise.resolve(t);
  }
  loadSkin(e) {
    const t = this.json.skins[e], i = [];
    for (let n = 0, s = t.joints.length; n < s; n++) i.push(this._loadNodeShallow(t.joints[n]));
    return t.inverseBindMatrices !== void 0 ? i.push(this.getDependency("accessor", t.inverseBindMatrices)) : i.push(null), Promise.all(i).then(function(n) {
      const s = n.pop(), a = n, o = [], l = [];
      for (let c = 0, h = a.length; c < h; c++) {
        const d = a[c];
        if (d) {
          o.push(d);
          const u = new Fe();
          s !== null && u.fromArray(s.array, c * 16), l.push(u);
        } else console.warn('THREE.GLTFLoader: Joint "%s" could not be found.', t.joints[c]);
      }
      return new Sn(o, l);
    });
  }
  loadAnimation(e) {
    const t = this.json, i = this, n = t.animations[e], s = n.name ? n.name : "animation_" + e, a = [], o = [], l = [], c = [], h = [];
    for (let d = 0, u = n.channels.length; d < u; d++) {
      const p = n.channels[d], f = n.samplers[p.sampler], v = p.target, m = v.node, g = n.parameters !== void 0 ? n.parameters[f.input] : f.input, E = n.parameters !== void 0 ? n.parameters[f.output] : f.output;
      v.node !== void 0 && (a.push(this.getDependency("node", m)), o.push(this.getDependency("accessor", g)), l.push(this.getDependency("accessor", E)), c.push(f), h.push(v));
    }
    return Promise.all([Promise.all(a), Promise.all(o), Promise.all(l), Promise.all(c), Promise.all(h)]).then(function(d) {
      const u = d[0], p = d[1], f = d[2], v = d[3], m = d[4], g = [];
      for (let R = 0, y = u.length; R < y; R++) {
        const S = u[R], T = p[R], A = f[R], x = v[R], b = m[R];
        if (S === void 0) continue;
        S.updateMatrix && S.updateMatrix();
        const N = i._createAnimationTracks(S, T, A, x, b);
        if (N) for (let O = 0; O < N.length; O++) g.push(N[O]);
      }
      const E = new hs(s, void 0, g);
      return Ri(E, n), E;
    });
  }
  createNodeMesh(e) {
    const t = this.json, i = this, n = t.nodes[e];
    return n.mesh === void 0 ? null : i.getDependency("mesh", n.mesh).then(function(s) {
      const a = i._getNodeRef(i.meshCache, n.mesh, s);
      return n.weights !== void 0 && a.traverse(function(o) {
        if (o.isMesh) for (let l = 0, c = n.weights.length; l < c; l++) o.morphTargetInfluences[l] = n.weights[l];
      }), a;
    });
  }
  loadNode(e) {
    const t = this.json, i = this, n = t.nodes[e], s = i._loadNodeShallow(e), a = [], o = n.children || [];
    for (let c = 0, h = o.length; c < h; c++) a.push(i.getDependency("node", o[c]));
    const l = n.skin === void 0 ? Promise.resolve(null) : i.getDependency("skin", n.skin);
    return Promise.all([s, Promise.all(a), l]).then(function(c) {
      const h = c[0], d = c[1], u = c[2];
      u !== null && h.traverse(function(p) {
        p.isSkinnedMesh && p.bind(u, aM);
      });
      for (let p = 0, f = d.length; p < f; p++) h.add(d[p]);
      if (h.userData.pivot !== void 0 && d.length > 0) {
        const p = h.userData.pivot, f = d[0];
        h.pivot = new P().fromArray(p), h.position.x -= p[0], h.position.y -= p[1], h.position.z -= p[2], f.position.set(0, 0, 0), delete h.userData.pivot;
      }
      return h;
    });
  }
  _loadNodeShallow(e) {
    const t = this.json, i = this.extensions, n = this;
    if (this.nodeCache[e] !== void 0) return this.nodeCache[e];
    const s = t.nodes[e], a = s.name ? n.createUniqueName(s.name) : "", o = [], l = n._invokeOne(function(c) {
      return c.createNodeMesh && c.createNodeMesh(e);
    });
    return l && o.push(l), s.camera !== void 0 && o.push(n.getDependency("camera", s.camera).then(function(c) {
      return n._getNodeRef(n.cameraCache, s.camera, c);
    })), n._invokeAll(function(c) {
      return c.createNodeAttachment && c.createNodeAttachment(e);
    }).forEach(function(c) {
      o.push(c);
    }), this.nodeCache[e] = Promise.all(o).then(function(c) {
      let h;
      if (s.isBone === !0 ? h = new ed() : c.length > 1 ? h = new Qt() : c.length === 1 ? h = c[0] : h = new at(), h !== c[0]) for (let d = 0, u = c.length; d < u; d++) h.add(c[d]);
      if (s.name && (h.userData.name = s.name, h.name = a), Ri(h, s), s.extensions && wr(i, h, s), s.matrix !== void 0) {
        const d = new Fe();
        d.fromArray(s.matrix), h.applyMatrix4(d);
      } else s.translation !== void 0 && h.position.fromArray(s.translation), s.rotation !== void 0 && h.quaternion.fromArray(s.rotation), s.scale !== void 0 && h.scale.fromArray(s.scale);
      if (!n.associations.has(h)) n.associations.set(h, {});
      else if (s.mesh !== void 0 && n.meshCache.refs[s.mesh] > 1) {
        const d = n.associations.get(h);
        n.associations.set(h, { ...d });
      }
      return n.associations.get(h).nodes = e, h;
    }), this.nodeCache[e];
  }
  loadScene(e) {
    const t = this.extensions, i = this.json.scenes[e], n = this, s = new Qt();
    i.name && (s.name = n.createUniqueName(i.name)), Ri(s, i), i.extensions && wr(t, s, i);
    const a = i.nodes || [], o = [];
    for (let l = 0, c = a.length; l < c; l++) o.push(n.getDependency("node", a[l]));
    return Promise.all(o).then(function(l) {
      for (let h = 0, d = l.length; h < d; h++) {
        const u = l[h];
        u.parent !== null ? s.add(Px(u)) : s.add(u);
      }
      const c = (h) => {
        const d = /* @__PURE__ */ new Map();
        for (const [u, p] of n.associations) (u instanceof vi || u instanceof Nt) && d.set(u, p);
        return h.traverse((u) => {
          const p = n.associations.get(u);
          p != null && d.set(u, p);
        }), d;
      };
      return n.associations = c(s), s;
    });
  }
  _createAnimationTracks(e, t, i, n, s) {
    const a = [], o = e.name ? e.name : e.uuid, l = [];
    function c(p) {
      p.morphTargetInfluences && l.push(p.name ? p.name : p.uuid);
    }
    cr[s.path] === cr.weights ? (c(e), e.isGroup && e.children.forEach(c)) : l.push(o);
    let h;
    switch (cr[s.path]) {
      case cr.weights:
        h = Mn;
        break;
      case cr.rotation:
        h = yn;
        break;
      case cr.translation:
      case cr.scale:
        h = fa;
        break;
      default:
        switch (i.itemSize) {
          case 1:
            h = Mn;
            break;
          case 2:
          case 3:
          default:
            h = fa;
            break;
        }
        break;
    }
    const d = n.interpolation !== void 0 ? eM[n.interpolation] : as, u = this._getArrayFromAccessor(i);
    for (let p = 0, f = l.length; p < f; p++) {
      const v = new h(l[p] + "." + cr[s.path], t.array, u, d);
      n.interpolation === "CUBICSPLINE" && this._createCubicSplineTrackInterpolant(v), a.push(v);
    }
    return a;
  }
  _getArrayFromAccessor(e) {
    let t = e.array;
    if (e.normalized) {
      const i = bl(t.constructor), n = new Float32Array(t.length);
      for (let s = 0, a = t.length; s < a; s++) n[s] = t[s] * i;
      t = n;
    }
    return t;
  }
  _createCubicSplineTrackInterpolant(e) {
    e.createInterpolant = function(t) {
      const i = this instanceof yn ? Jx : Ed;
      return new i(this.times, this.values, this.getValueSize() / 3, t);
    }, e.createInterpolant.isInterpolantFactoryMethodGLTFCubicSpline = !0;
  }
}
function lM(r, e, t) {
  const i = e.attributes, n = new er();
  if (i.POSITION !== void 0) {
    const o = t.json.accessors[i.POSITION], l = o.min, c = o.max;
    if (l !== void 0 && c !== void 0) {
      if (n.set(new P(l[0], l[1], l[2]), new P(c[0], c[1], c[2])), o.normalized) {
        const h = bl(mn[o.componentType]);
        n.min.multiplyScalar(h), n.max.multiplyScalar(h);
      }
    } else {
      console.warn("THREE.GLTFLoader: Missing min/max properties for accessor POSITION.");
      return;
    }
  } else return;
  const s = e.targets;
  if (s !== void 0) {
    const o = new P(), l = new P();
    for (let c = 0, h = s.length; c < h; c++) {
      const d = s[c];
      if (d.POSITION !== void 0) {
        const u = t.json.accessors[d.POSITION], p = u.min, f = u.max;
        if (p !== void 0 && f !== void 0) {
          if (l.setX(Math.max(Math.abs(p[0]), Math.abs(f[0]))), l.setY(Math.max(Math.abs(p[1]), Math.abs(f[1]))), l.setZ(Math.max(Math.abs(p[2]), Math.abs(f[2]))), u.normalized) {
            const v = bl(mn[u.componentType]);
            l.multiplyScalar(v);
          }
          o.max(l);
        } else console.warn("THREE.GLTFLoader: Missing min/max properties for accessor POSITION.");
      }
    }
    n.expandByVector(o);
  }
  r.boundingBox = n;
  const a = new Di();
  n.getCenter(a.center), a.radius = n.min.distanceTo(n.max) / 2, r.boundingSphere = a;
}
function Ah(r, e, t) {
  const i = e.attributes, n = [];
  function s(a, o) {
    return t.getDependency("accessor", a).then(function(l) {
      r.setAttribute(o, l);
    });
  }
  for (const a in i) {
    const o = El[a] || a.toLowerCase();
    o in r.attributes || n.push(s(i[a], o));
  }
  if (e.indices !== void 0 && !r.index) {
    const a = t.getDependency("accessor", e.indices).then(function(o) {
      r.setIndex(o);
    });
    n.push(a);
  }
  return Ze.workingColorSpace !== ei && "COLOR_0" in i && console.warn(`THREE.GLTFLoader: Converting vertex colors from "srgb-linear" to "${Ze.workingColorSpace}" not supported.`), Ri(r, e), lM(r, e, t), Promise.all(n).then(function() {
    return e.targets !== void 0 ? iM(r, e.targets, t) : r;
  });
}
var Rh = (r, e, t) => new Promise((i, n) => {
  var s = (l) => {
    try {
      o(t.next(l));
    } catch (c) {
      n(c);
    }
  }, a = (l) => {
    try {
      o(t.throw(l));
    } catch (c) {
      n(c);
    }
  }, o = (l) => l.done ? i(l.value) : Promise.resolve(l.value).then(s, a);
  o((t = t.apply(r, e)).next());
}), cM = { Aa: "aa", Ih: "ih", Ou: "ou", Ee: "ee", Oh: "oh", Blink: "blink", Happy: "happy", Angry: "angry", Sad: "sad", Relaxed: "relaxed", LookUp: "lookUp", Surprised: "surprised", LookDown: "lookDown", LookLeft: "lookLeft", LookRight: "lookRight", BlinkLeft: "blinkLeft", BlinkRight: "blinkRight", Neutral: "neutral" };
new be();
var Ph = { hips: null, spine: "hips", chest: "spine", upperChest: "chest", neck: "upperChest", head: "neck", leftEye: "head", rightEye: "head", jaw: "head", leftUpperLeg: "hips", leftLowerLeg: "leftUpperLeg", leftFoot: "leftLowerLeg", leftToes: "leftFoot", rightUpperLeg: "hips", rightLowerLeg: "rightUpperLeg", rightFoot: "rightLowerLeg", rightToes: "rightFoot", leftShoulder: "upperChest", leftUpperArm: "leftShoulder", leftLowerArm: "leftUpperArm", leftHand: "leftLowerArm", rightShoulder: "upperChest", rightUpperArm: "rightShoulder", rightLowerArm: "rightUpperArm", rightHand: "rightLowerArm", leftThumbMetacarpal: "leftHand", leftThumbProximal: "leftThumbMetacarpal", leftThumbDistal: "leftThumbProximal", leftIndexProximal: "leftHand", leftIndexIntermediate: "leftIndexProximal", leftIndexDistal: "leftIndexIntermediate", leftMiddleProximal: "leftHand", leftMiddleIntermediate: "leftMiddleProximal", leftMiddleDistal: "leftMiddleIntermediate", leftRingProximal: "leftHand", leftRingIntermediate: "leftRingProximal", leftRingDistal: "leftRingIntermediate", leftLittleProximal: "leftHand", leftLittleIntermediate: "leftLittleProximal", leftLittleDistal: "leftLittleIntermediate", rightThumbMetacarpal: "rightHand", rightThumbProximal: "rightThumbMetacarpal", rightThumbDistal: "rightThumbProximal", rightIndexProximal: "rightHand", rightIndexIntermediate: "rightIndexProximal", rightIndexDistal: "rightIndexIntermediate", rightMiddleProximal: "rightHand", rightMiddleIntermediate: "rightMiddleProximal", rightMiddleDistal: "rightMiddleIntermediate", rightRingProximal: "rightHand", rightRingIntermediate: "rightRingProximal", rightRingDistal: "rightRingIntermediate", rightLittleProximal: "rightHand", rightLittleIntermediate: "rightLittleProximal", rightLittleDistal: "rightLittleIntermediate" };
function hM(r) {
  return r.invert ? r.invert() : r.inverse(), r;
}
var uM = new P(), dM = new P();
function pM(r, e) {
  return r.matrixWorld.decompose(uM, e, dM), e;
}
function go(r) {
  return [Math.atan2(-r.z, r.x), Math.atan2(r.y, Math.sqrt(r.x * r.x + r.z * r.z))];
}
function Ch(r) {
  const e = Math.round(r / 2 / Math.PI);
  return r - 2 * Math.PI * e;
}
var Lh = new P(0, 0, 1), fM = new P(), mM = new P(), gM = new P(), _M = new Ce(), _o = new Ce(), Ih = new Ce(), vM = new Ce(), vo = new Yt(), bd = class wd {
  constructor(e, t) {
    this.offsetFromHeadBone = new P(), this.autoUpdate = !0, this.faceFront = new P(0, 0, 1), this.humanoid = e, this.applier = t, this._yaw = 0, this._pitch = 0, this._needsUpdate = !0, this._restHeadWorldQuaternion = this.getLookAtWorldQuaternion(new Ce());
  }
  get yaw() {
    return this._yaw;
  }
  set yaw(e) {
    this._yaw = e, this._needsUpdate = !0;
  }
  get pitch() {
    return this._pitch;
  }
  set pitch(e) {
    this._pitch = e, this._needsUpdate = !0;
  }
  get euler() {
    return console.warn("VRMLookAt: euler is deprecated. use getEuler() instead."), this.getEuler(new Yt());
  }
  getEuler(e) {
    return e.set(mt.DEG2RAD * this._pitch, mt.DEG2RAD * this._yaw, 0, "YXZ");
  }
  copy(e) {
    if (this.humanoid !== e.humanoid) throw new Error("VRMLookAt: humanoid must be same in order to copy");
    return this.offsetFromHeadBone.copy(e.offsetFromHeadBone), this.applier = e.applier, this.autoUpdate = e.autoUpdate, this.target = e.target, this.faceFront.copy(e.faceFront), this;
  }
  clone() {
    return new wd(this.humanoid, this.applier).copy(this);
  }
  reset() {
    this._yaw = 0, this._pitch = 0, this._needsUpdate = !0;
  }
  getLookAtWorldPosition(e) {
    const t = this.humanoid.getRawBoneNode("head");
    return e.copy(this.offsetFromHeadBone).applyMatrix4(t.matrixWorld);
  }
  getLookAtWorldQuaternion(e) {
    const t = this.humanoid.getRawBoneNode("head");
    return pM(t, e);
  }
  getFaceFrontQuaternion(e) {
    if (this.faceFront.distanceToSquared(Lh) < 0.01) return e.copy(this._restHeadWorldQuaternion).invert();
    const [t, i] = go(this.faceFront);
    return vo.set(0, 0.5 * Math.PI + t, i, "YZX"), e.setFromEuler(vo).premultiply(vM.copy(this._restHeadWorldQuaternion).invert());
  }
  getLookAtWorldDirection(e) {
    return this.getLookAtWorldQuaternion(_o), this.getFaceFrontQuaternion(Ih), e.copy(Lh).applyQuaternion(_o).applyQuaternion(Ih).applyEuler(this.getEuler(vo));
  }
  lookAt(e) {
    const t = _M.copy(this._restHeadWorldQuaternion).multiply(hM(this.getLookAtWorldQuaternion(_o))), i = this.getLookAtWorldPosition(mM), n = gM.copy(e).sub(i).applyQuaternion(t).normalize(), [s, a] = go(this.faceFront), [o, l] = go(n), c = Ch(o - s), h = Ch(a - l);
    this._yaw = mt.RAD2DEG * c, this._pitch = mt.RAD2DEG * h, this._needsUpdate = !0;
  }
  update(e) {
    this.target != null && this.autoUpdate && this.lookAt(this.target.getWorldPosition(fM)), this._needsUpdate && (this._needsUpdate = !1, this.applier.applyYawPitch(this._yaw, this._pitch));
  }
};
bd.EULER_ORDER = "YXZ";
var xM = bd, Nh = 180 / Math.PI, xo = new Yt(), wl = class extends at {
  constructor(r) {
    super(), this.vrmLookAt = r, this.type = "VRMLookAtQuaternionProxy";
    const e = this.rotation._onChangeCallback;
    this.rotation._onChange(() => {
      e(), this._applyToLookAt();
    });
    const t = this.quaternion._onChangeCallback;
    this.quaternion._onChange(() => {
      t(), this._applyToLookAt();
    });
  }
  _applyToLookAt() {
    xo.setFromQuaternion(this.quaternion, xM.EULER_ORDER), this.vrmLookAt.yaw = Nh * xo.y, this.vrmLookAt.pitch = Nh * xo.x;
  }
};
function MM(r, e, t) {
  var i, n;
  const s = /* @__PURE__ */ new Map(), a = /* @__PURE__ */ new Map();
  for (const [o, l] of r.humanoidTracks.rotation.entries()) {
    const c = (i = e.getNormalizedBoneNode(o)) == null ? void 0 : i.name;
    if (c != null) {
      const h = new yn(`${c}.quaternion`, l.times, l.values.map((d, u) => t === "0" && u % 2 === 0 ? -d : d));
      a.set(o, h);
    }
  }
  for (const [o, l] of r.humanoidTracks.translation.entries()) {
    const c = (n = e.getNormalizedBoneNode(o)) == null ? void 0 : n.name;
    if (c != null) {
      const h = r.restHipsPosition.y, d = e.normalizedRestPose.hips.position[1] / h, u = l.clone();
      u.values = u.values.map((p, f) => (t === "0" && f % 3 !== 1 ? -p : p) * d), u.name = `${c}.position`, s.set(o, u);
    }
  }
  return { translation: s, rotation: a };
}
function yM(r, e) {
  const t = /* @__PURE__ */ new Map(), i = /* @__PURE__ */ new Map();
  for (const [n, s] of r.expressionTracks.preset.entries()) {
    const a = e.getExpressionTrackName(n);
    if (a != null) {
      const o = s.clone();
      o.name = a, t.set(n, o);
    }
  }
  for (const [n, s] of r.expressionTracks.custom.entries()) {
    const a = e.getExpressionTrackName(n);
    if (a != null) {
      const o = s.clone();
      o.name = a, i.set(n, o);
    }
  }
  return { preset: t, custom: i };
}
function SM(r, e) {
  if (r.lookAtTrack == null) return null;
  const t = r.lookAtTrack.clone();
  return t.name = e, t;
}
function TM(r, e) {
  const t = [], i = MM(r, e.humanoid, e.meta.metaVersion);
  if (t.push(...i.translation.values()), t.push(...i.rotation.values()), e.expressionManager != null) {
    const n = yM(r, e.expressionManager);
    t.push(...n.preset.values()), t.push(...n.custom.values());
  }
  if (e.lookAt != null) {
    let n = e.scene.children.find((a) => a instanceof wl);
    n == null ? (console.warn("createVRMAnimationClip: VRMLookAtQuaternionProxy is not found. Creating a new one automatically. To suppress this warning, create a VRMLookAtQuaternionProxy manually"), n = new wl(e.lookAt), n.name = "VRMLookAtQuaternionProxy", e.scene.add(n)) : n.name === "" && (console.warn("createVRMAnimationClip: VRMLookAtQuaternionProxy is found but its name is not set. Setting the name automatically. To suppress this warning, set the name manually"), n.name = "VRMLookAtQuaternionProxy");
    const s = SM(r, `${n.name}.quaternion`);
    s != null && t.push(s);
  }
  return new hs("Clip", r.duration, t);
}
var EM = class {
  constructor() {
    this.duration = 0, this.restHipsPosition = new P(), this.humanoidTracks = { translation: /* @__PURE__ */ new Map(), rotation: /* @__PURE__ */ new Map() }, this.expressionTracks = { preset: /* @__PURE__ */ new Map(), custom: /* @__PURE__ */ new Map() }, this.lookAtTrack = null;
  }
};
function Uh(r, e) {
  const t = r.length, i = [];
  let n = [], s = 0;
  for (let a = 0; a < t; a++) {
    const o = r[a];
    s <= 0 && (s = e, n = [], i.push(n)), n.push(o), s--;
  }
  return i;
}
var bM = new Fe(), Hn = new P(), Mo = new Ce(), Dh = new Ce(), wM = new Ce(), AM = /* @__PURE__ */ new Set(["1.0", "1.0-draft"]), RM = new Set(Object.values(cM)), PM = class {
  constructor(r) {
    this.parser = r;
  }
  get name() {
    return "VRMC_vrm_animation";
  }
  afterRoot(r) {
    return Rh(this, null, function* () {
      var e, t, i;
      const n = r.parser.json, s = n.extensionsUsed;
      if (s == null || s.indexOf(this.name) == -1) return;
      const a = (e = n.extensions) == null ? void 0 : e[this.name];
      if (a == null) return;
      const o = a.specVersion;
      if (o == null) console.warn("VRMAnimationLoaderPlugin: specVersion of the VRMA is not defined. Consider updating the animation file. Assuming the spec version is 1.0.");
      else {
        if (!AM.has(o)) {
          console.warn(`VRMAnimationLoaderPlugin: Unknown VRMC_vrm_animation spec version: ${o}`);
          return;
        }
        o === "1.0-draft" && console.warn("VRMAnimationLoaderPlugin: Using a draft spec version: 1.0-draft. Some behaviors may be different. Consider updating the animation file.");
      }
      const l = this._createNodeMap(a), c = yield this._createBoneWorldMatrixMap(r, a), h = (i = (t = a.humanoid) == null ? void 0 : t.humanBones.hips) == null ? void 0 : i.node, d = h != null ? yield r.parser.getDependency("node", h) : null, u = new P();
      d?.getWorldPosition(u), u.y < 1e-3 && console.warn("VRMAnimationLoaderPlugin: The loaded VRM Animation might violate the VRM T-pose (The y component of the rest hips position is approximately zero or below.)");
      const p = r.animations.map((f, v) => {
        const m = n.animations[v], g = this._parseAnimation(f, m, l, c);
        return g.restHipsPosition = u, g;
      });
      r.userData.vrmAnimations = p;
    });
  }
  _createNodeMap(r) {
    var e, t, i, n, s;
    const a = /* @__PURE__ */ new Map(), o = /* @__PURE__ */ new Map(), l = (e = r.humanoid) == null ? void 0 : e.humanBones;
    l && Object.entries(l).forEach(([u, p]) => {
      const f = p?.node;
      f != null && a.set(f, u);
    });
    const c = (t = r.expressions) == null ? void 0 : t.preset;
    c && Object.entries(c).forEach(([u, p]) => {
      const f = p?.node;
      f != null && o.set(f, u);
    });
    const h = (i = r.expressions) == null ? void 0 : i.custom;
    h && Object.entries(h).forEach(([u, p]) => {
      const { node: f } = p;
      o.set(f, u);
    });
    const d = (s = (n = r.lookAt) == null ? void 0 : n.node) != null ? s : null;
    return { humanoidIndexToName: a, expressionsIndexToName: o, lookAtIndex: d };
  }
  _createBoneWorldMatrixMap(r, e) {
    return Rh(this, null, function* () {
      var t, i;
      r.scene.updateWorldMatrix(!1, !0);
      const n = yield r.parser.getDependencies("node"), s = /* @__PURE__ */ new Map();
      if (e.humanoid == null) return s;
      for (const [a, o] of Object.entries(e.humanoid.humanBones)) {
        const l = o?.node;
        if (l != null) {
          const c = n[l];
          s.set(a, c.matrixWorld), a === "hips" && s.set("hipsParent", (i = (t = c.parent) == null ? void 0 : t.matrixWorld) != null ? i : bM);
        }
      }
      return s;
    });
  }
  _parseAnimation(r, e, t, i) {
    const n = r.tracks, s = e.channels, a = new EM();
    return a.duration = r.duration, s.forEach((o, l) => {
      const { node: c, path: h } = o.target, d = n[l];
      if (c == null) return;
      const u = t.humanoidIndexToName.get(c);
      if (u != null) {
        let f = Ph[u];
        for (; f != null && i.get(f) == null; ) f = Ph[f];
        if (f == null && (f = "hipsParent"), h === "translation") if (u !== "hips") console.warn(`The loading animation contains a translation track for ${u}, which is not permitted in the VRMC_vrm_animation spec. ignoring the track`);
        else {
          const v = i.get("hipsParent"), m = Uh(d.values, 3).flatMap((E) => Hn.fromArray(E).applyMatrix4(v).toArray()), g = d.clone();
          g.values = new Float32Array(m), a.humanoidTracks.translation.set(u, g);
        }
        else if (h === "rotation") {
          const v = i.get(u), m = i.get(f);
          v.decompose(Hn, Mo, Hn), Mo.invert(), m.decompose(Hn, Dh, Hn);
          const g = Uh(d.values, 4).flatMap((R) => wM.fromArray(R).premultiply(Dh).multiply(Mo).toArray()), E = d.clone();
          E.values = new Float32Array(g), a.humanoidTracks.rotation.set(u, E);
        } else throw new Error(`Invalid path "${h}"`);
        return;
      }
      const p = t.expressionsIndexToName.get(c);
      if (p != null) {
        if (h === "translation") {
          const f = d.times, v = new Float32Array(d.values.length / 3);
          for (let g = 0; g < v.length; g++) v[g] = d.values[3 * g];
          const m = new Mn(`${p}.weight`, f, v);
          RM.has(p) ? a.expressionTracks.preset.set(p, m) : a.expressionTracks.custom.set(p, m);
        } else throw new Error(`Invalid path "${h}"`);
        return;
      }
      if (c === t.lookAtIndex) if (h === "rotation") a.lookAtTrack = d;
      else throw new Error(`Invalid path "${h}"`);
    }), a;
  }
};
const yo = /* @__PURE__ */ new Map();
function CM(r) {
  if (r.startsWith("http://") || r.startsWith("https://") || r.startsWith("blob:") || r.startsWith("data:")) return r;
  if (typeof window < "u" && window.location?.origin && window.location.origin !== "null") try {
    return new URL(r, window.location.origin).href;
  } catch {
    return r;
  }
  return r;
}
async function LM(r) {
  const e = CM(r), t = yo.get(e);
  if (t) return t;
  const i = (async () => {
    const n = new Sd();
    n.register((a) => ({ name: "VRMC_vrm_animation_spec_patch", beforeRoot: async () => {
      const o = a.json?.extensions?.VRMC_vrm_animation;
      o && !o.specVersion && (o.specVersion = "1.0");
    } })), n.register((a) => new PM(a));
    const s = (await n.loadAsync(e)).userData.vrmAnimations?.[0];
    if (!s) throw new Error(`No VRMAnimation found in ${r}`);
    return s;
  })();
  yo.set(e, i);
  try {
    return await i;
  } catch (n) {
    throw yo.delete(e), n;
  }
}
async function IM(r, e) {
  if (e.meta || (e.meta = { metaVersion: "0" }), e.lookAt && e.scene && !e.scene.children.some((o) => o.name === "VRMLookAtQuaternionProxy")) {
    const o = new wl(e.lookAt);
    o.name = "VRMLookAtQuaternionProxy", e.scene.add(o);
  }
  const t = await LM(r), i = TM(t, e), n = i.tracks.filter((o) => !(o.name.includes(".weight") || o.name.includes("VRMLookAtQuaternionProxy") || o.name.includes("leftEye") || o.name.includes("rightEye"))), s = i.duration > 0 ? i.duration : -1, a = new hs(i.name || r, s, n);
  return a.duration <= 0 && a.resetDuration(), a;
}
async function Oh(r, e) {
  try {
    return await IM(r, e);
  } catch (t) {
    return (typeof process > "u" || process.env.NODE_ENV !== "test") && console.warn(`[VRMA] Failed to load animation clip from ${r}:`, t), null;
  }
}
function NM(r) {
  return Number.isFinite(r) ? Math.max(0, Math.min(r, 0.05)) : 0;
}
function it(r, e, t, i = 6) {
  return r + (e - r) * (1 - Math.exp(-i * t));
}
const Dt = { GREETING: "/animations/VRMA_02.vrma", BASE_IDLE: "/animations/idle.vrma", IDLE: "/animations/idle.vrma", MODEL_POSE: "/animations/idle.vrma", VRMA_06: "/animations/VRMA_06.vrma", SMARTPHONE: "/animations/005_smartphone.vrma", HELLO: "/animations/004_hello_1.vrma", DRINK_WATER: "/animations/006_drinkwater.vrma", MOTION_POSE: "/animations/001_motion_pose.vrma", PEACE: "/animations/VRMA_03.vrma", SHOOT: "/animations/VRMA_04.vrma" }, Fh = [{ url: Dt.GREETING, weight: 0.8 }, { url: Dt.HELLO, weight: 0.2 }], So = [{ url: Dt.BASE_IDLE, weight: 0.7495 }, { url: Dt.SMARTPHONE, weight: 0.23 }, { url: Dt.DRINK_WATER, weight: 0.02 }, { url: Dt.MOTION_POSE, weight: 5e-4 }], UM = 120;
function qi(r) {
  return r === Dt.BASE_IDLE || r === Dt.MODEL_POSE || r === Dt.IDLE;
}
function kn(r, e) {
  return qi(r) ? Math.max(e * 2, UM) : e;
}
function To(r, e = Math.random) {
  if (!r.length) return "";
  const t = r.reduce((s, a) => s + a.weight, 0), i = e() * t;
  let n = 0;
  for (let s = 0; s < r.length; s++) if (n += r[s].weight, i <= n + 1e-9 || s === r.length - 1) return r[s].url;
  return r[r.length - 1].url;
}
function DM(r, e = Math.random) {
  let t = 0, i = 3 + e() * 3, n = 0, s = 0, a = 0.25, o = 0, l = 0, c = 0, h = 0, d = 0, u = 0, p = 0, f = 0, v = 0, m = 0;
  const g = new at();
  g.name = "VRMMouseLookAtTarget";
  const E = r.humanoid?.getNormalizedBoneNode("head");
  r.scene?.updateMatrixWorld(!0);
  const R = new P();
  E?.getWorldPosition(R);
  const y = R.y > 0.5 ? R.y : 1.45;
  g.position.set(0, y, 1.5), r.scene?.add(g), r.lookAt && (r.lookAt.target = g, r.lookAt.autoUpdate = !0);
  const S = new P(), T = new Ce(), A = new Ce(), x = new P();
  let b = 0, N = 0, O = 0, k = 0, q = null, B;
  const G = () => {
    B && (clearTimeout(B), B = void 0), q = null;
  }, Z = () => {
    const ae = q;
    G(), ae?.();
  }, X = r.scene ?? r.humanoid?.getNormalizedBoneNode("hips")?.parent ?? new at(), te = new Rm(X);
  let V = null, K = "", ee = 0, Ue = 0, ue = null, Ye = 0, ke = !1, je = !1, Y = !1, $ = !1, le = !1, Oe = !1, xe = 0, Te = 14 + e() * 14;
  const He = /* @__PURE__ */ new Map(), Ke = [Dt.SHOOT], Ve = [...Fh.map((ae) => ae.url), ...So.map((ae) => ae.url), ...Ke], vt = () => {
    je = !1, ue && (ue.fadeOut(0.6), V && !le && (V.reset().setEffectiveWeight(1).fadeIn(0.6).play(), ee = t), ue = null);
  }, Mt = () => {
    if (qi(K)) return;
    const ae = He.get(Dt.BASE_IDLE);
    if (ae) {
      const J = te.clipAction(ae);
      J.reset().setLoop(Kn, 1 / 0), J.clampWhenFinished = !1, J.timeScale = 0.98 + e() * 0.04, J.setEffectiveWeight(1).fadeIn(0.6).play(), V && V !== J && V.fadeOut(0.6), V = J, K = Dt.BASE_IDLE, ee = t, Ue = kn(Dt.BASE_IDLE, ae.duration);
    }
  }, dt = (ae) => {
    if ($) return;
    const J = ae;
    if (ue && J.action === ue) {
      if (ke) return;
      vt(), Z();
    } else V && J.action === V && !qi(K) && Mt();
  };
  te.addEventListener("finished", dt), (async () => {
    try {
      for (const Le of Ve) {
        const Ne = await Oh(Le, r);
        if ($) return;
        Ne && He.set(Le, Ne);
      }
      const ae = To(Fh, e), J = He.get(ae) ?? He.get(Dt.GREETING);
      if (J && b === 0) {
        const Le = te.clipAction(J);
        Le.reset().setLoop(sn, 1), Le.clampWhenFinished = !0, Le.setEffectiveWeight(1).fadeIn(0.2).play(), ue = Le, Ye = Math.max(1, J.duration), Y = !0, je = !0;
      }
      const me = b > 0 ? Dt.BASE_IDLE : To(So, e), pe = He.get(me) ?? He.get(Dt.BASE_IDLE);
      if (pe && !V) {
        const Le = te.clipAction(pe);
        qi(me) ? (Le.reset().setLoop(Kn, 1 / 0), Le.clampWhenFinished = !1) : (Le.reset().setLoop(sn, 1), Le.clampWhenFinished = !0), Le.timeScale = 1 + (e() * 0.03 - 0.015), ue || le ? Le.setEffectiveWeight(0).play() : Le.setEffectiveWeight(1).fadeIn(0.2).play(), V = Le, K = me, ee = 0, Ue = kn(me, pe.duration), Y = !0;
      }
    } catch {
    }
  })();
  const Je = r.humanoid?.getNormalizedBoneNode("chest") ?? r.humanoid?.getNormalizedBoneNode("spine"), At = Je?.rotation.x ?? 0, tt = r.humanoid?.getNormalizedBoneNode("neck"), _t = tt?.rotation.x ?? 0, C = r.humanoid?.getNormalizedBoneNode("head"), xt = C?.rotation.x ?? 0, Qe = C?.rotation.y ?? 0, w = C?.rotation.z ?? 0, _ = r.humanoid?.getNormalizedBoneNode("leftUpperArm"), F = r.humanoid?.getNormalizedBoneNode("rightUpperArm"), W = r.humanoid?.getNormalizedBoneNode("leftLowerArm"), j = r.humanoid?.getNormalizedBoneNode("rightLowerArm"), oe = r.humanoid?.getNormalizedBoneNode("leftHand"), he = r.humanoid?.getNormalizedBoneNode("rightHand");
  let D = 6 + e() * 8, se = !1, ce = 0, Me = 3.5, ie = "explain_right";
  const ge = r.meta?.metaVersion === "1" ? -1 : 1;
  let Se = [0.08, 0, 1.25 * ge], Pe = [0.08, 0, -1.25 * ge], Ie = [0, 0.15, 0.1 * ge], U = [0, -0.15, -0.1 * ge], Q = [0.05, 0.1, 0.05 * ge], re = [0.05, -0.1, -0.05 * ge];
  const _e = (ae, J, me = "idle", pe, Le = !1, Ne, Ut, Xt = "unknown") => {
    if ($) return;
    C && C !== Je && (C.rotation.x -= N, C.rotation.y -= O, C.rotation.z -= k, N = O = k = 0);
    const Xe = NM(ae);
    t += Xe, le = me === "sleeping", le !== Oe && (Oe = le, le ? (++b, G(), V?.fadeOut(1.2), ue?.fadeOut(0.8), ue = null) : (V?.reset().setEffectiveWeight(1).fadeIn(0.8).play(), ee = t)), t > xe && (xe = t + 6 + e() * 6, V && !le && (V.timeScale = 0.98 + e() * 0.04));
    const Mi = me === "speaking" || me === "speaking_alt";
    if (Mi && !ue && !le && t >= Te && qi(K)) if (e() < 0.2) {
      const M = Ke.filter((I) => He.has(I));
      if (M.length > 0 && V) {
        const I = M[Math.floor(e() * M.length)], z = He.get(I), L = te.clipAction(z);
        L.reset().setLoop(sn, 1), L.clampWhenFinished = !0, L.timeScale = 0.98 + e() * 0.04, L.setEffectiveWeight(0.6).fadeIn(0.6).play(), V.fadeOut(0.6), ue = L, Ye = z.duration, Te = t + Ye + 14 + e() * 14;
      } else Te = t + 14 + e() * 14;
    } else Te = t + 14 + e() * 14;
    if (!ue && V && !le && t >= ee + Ue) if (!qi(K)) Mt();
    else if (me === "idle") {
      const M = To(So, e), I = He.get(M) ?? He.get(Dt.BASE_IDLE);
      if (I) {
        const z = te.clipAction(I);
        z === V ? (z.setEffectiveWeight(1), z.isRunning() || z.play(), ee = t, Ue = kn(M, I.duration)) : (qi(M) ? (z.reset().setLoop(Kn, 1 / 0), z.clampWhenFinished = !1) : (z.reset().setLoop(sn, 1), z.clampWhenFinished = !0), z.timeScale = 0.98 + e() * 0.04, z.setEffectiveWeight(1).fadeIn(0.6).play(), V.fadeOut(0.6), V = z, K = M, ee = t, Ue = kn(M, I.duration));
      }
    } else ee = t;
    if (te.update(Xe), !Y || le) {
      const M = le ? 1.1 : me === "angry" ? 2.6 : 1.6, I = le ? 0.016 : me === "angry" ? 0.018 : 0.012;
      Je && (Je.rotation.x = At + Math.sin(t * M) * I);
      const z = Math.sin(t * M) * 0.012;
      if (le) se = !1, D = t + 10 + e() * 10;
      else if (se) t >= ce + Me && (se = !1);
      else if (t >= D) if (se = !0, ce = t, Me = 3.2 + e() * 1.2, Mi) {
        const ze = e();
        ie = ze < 0.45 ? "explain_right" : ze < 0.8 ? "explain_left" : "ponder", D = t + Me + 14 + e() * 16;
      } else me === "thinking" ? (ie = "ponder", D = t + Me + 18 + e() * 18) : (ie = "rest_shift", D = t + Me + 25 + e() * 25);
      let L = 0;
      if (se) {
        const ze = Math.max(0, Math.min(1, (t - ce) / Me));
        L = Math.sin(ze * Math.PI);
      }
      let H = [0.08, 0, 1.25 * ge], ne = [0.08, 0, -1.25 * ge], de = [0, 0.15, 0.1 * ge], ve = [0, -0.15, -0.1 * ge], ye = [0.05, 0.1, 0.05 * ge], we = [0.05, -0.1, -0.05 * ge];
      if (L > 1e-3) switch (ie) {
        case "explain_right":
          ne = [0.08 + 0.16 * L, 0.08 * L, (-1.25 + 0.22 * L) * ge], ve = [0.32 * L, -0.15 - 0.12 * L, -0.1 * ge], we = [0.05 + 0.12 * L, -0.1 - 0.12 * L, (-0.05 - 0.08 * L) * ge];
          break;
        case "explain_left":
          H = [0.08 + 0.16 * L, -0.08 * L, (1.25 - 0.22 * L) * ge], de = [0.32 * L, 0.15 + 0.12 * L, 0.1 * ge], ye = [0.05 + 0.12 * L, 0.1 + 0.12 * L, (0.05 + 0.08 * L) * ge];
          break;
        case "ponder":
          ne = [0.08 + 0.12 * L, 0.04 * L, (-1.25 + 0.15 * L) * ge], ve = [0.42 * L, -0.15 - 0.18 * L, (-0.1 - 0.05 * L) * ge], we = [0.05 + 0.15 * L, -0.1 - 0.08 * L, -0.05 * ge];
          break;
        case "rest_shift":
          H = [0.09 * L + 0.08 * (1 - L), 0, (1.23 * L + 1.25 * (1 - L)) * ge], ne = [0.07 * L + 0.08 * (1 - L), 0, (-1.27 * L + -1.25 * (1 - L)) * ge], de = [0.04 * L, 0.18 * L + 0.15 * (1 - L), 0.1 * ge], ve = [-0.02 * L, -0.13 * L + -0.15 * (1 - L), -0.1 * ge], ye = [0.07 * L + 0.05 * (1 - L), 0.12 * L + 0.1 * (1 - L), 0.05 * ge], we = [0.04 * L + 0.05 * (1 - L), -0.08 * L + -0.1 * (1 - L), -0.05 * ge];
          break;
      }
      Se = [it(Se[0], H[0], Xe, 5), it(Se[1], H[1], Xe, 5), it(Se[2], H[2], Xe, 5)], Pe = [it(Pe[0], ne[0], Xe, 5), it(Pe[1], ne[1], Xe, 5), it(Pe[2], ne[2], Xe, 5)], Ie = [it(Ie[0], de[0], Xe, 5), it(Ie[1], de[1], Xe, 5), it(Ie[2], de[2], Xe, 5)], U = [it(U[0], ve[0], Xe, 5), it(U[1], ve[1], Xe, 5), it(U[2], ve[2], Xe, 5)], Q = [it(Q[0], ye[0], Xe, 5), it(Q[1], ye[1], Xe, 5), it(Q[2], ye[2], Xe, 5)], re = [it(re[0], we[0], Xe, 5), it(re[1], we[1], Xe, 5), it(re[2], we[2], Xe, 5)], _ && _ !== Je && _.rotation.set(Se[0], Se[1], Se[2] - z * ge), F && F !== Je && F.rotation.set(Pe[0], Pe[1], Pe[2] + z * ge), W && W !== Je && W !== _ && W.rotation.set(Ie[0], Ie[1], Ie[2]), j && j !== Je && j !== F && j.rotation.set(U[0], U[1], U[2]), oe && oe !== Je && oe !== _ && oe !== W && oe.rotation.set(Q[0], Q[1], Q[2]), he && he !== Je && he !== F && he !== j && he.rotation.set(re[0], re[1], re[2]);
    }
    let jt = 0, qt = 0, ti = 0;
    switch (me) {
      case "sleeping":
        jt = 0.16, qt = 0.02, ti = 0.06;
        break;
      case "listening":
        jt = 0.05, qt = 0.03, ti = 0.07;
        break;
      case "thinking":
        jt = -0.07, qt = 0.09, ti = -0.04;
        break;
      case "speaking":
      case "speaking_alt": {
        jt = Math.sin(t * 4.2) * (J * 0.045), qt = Math.sin(t * 2.1) * 0.03, ti = Math.sin(t * 1.5) * 0.015;
        break;
      }
      case "angry":
        jt = 0.1, qt = 0, ti = 0;
        break;
      case "error":
        jt = 0.12, qt = 0, ti = -0.05;
        break;
      case "idle":
      default:
        jt = Math.sin(t * 0.8) * 0.015, qt = Math.sin(t * 0.5) * 0.02, ti = Math.sin(t * 0.6) * 0.01;
        break;
    }
    const gr = !le && !!pe?.active, _r = gr ? Math.max(-1.8, Math.min(1.8, pe?.x ?? 0)) : 0, ci = gr ? Math.max(-1.8, Math.min(1.8, pe?.y ?? 0)) : 0;
    p = it(p, _r, Xe, 4), f = it(f, ci, Xe, 4);
    const vr = p * 0.12, Fi = -f * 0.08;
    v = it(v, vr, Xe, 4), m = it(m, Fi, Xe, 4), h = it(h, jt, Xe, 5), d = it(d, qt, Xe, 5), u = it(u, ti, Xe, 5);
    const Bi = !!(ue || V && !qi(K));
    if (C && C !== Je) {
      const M = !!(V?.isRunning() || ue?.isRunning() || ke && ue);
      if (Y && !le && M) {
        const I = C.rotation.x, z = C.rotation.y, L = C.rotation.z, H = Bi ? 0 : h + m, ne = Bi ? 0 : d + v, de = Bi ? 0 : u;
        C.rotation.x += H, C.rotation.y += ne, C.rotation.z += de, C.rotation.x = I + Math.max(-0.22, Math.min(0.35, H)), C.rotation.y = z + Math.max(-0.65, Math.min(0.65, ne)), C.rotation.z = L + Math.max(-0.12, Math.min(0.12, de)), N = C.rotation.x - I, O = C.rotation.y - z, k = C.rotation.z - L;
      } else {
        N = 0, O = 0, k = 0, C.rotation.x = xt + h + (le ? 0 : m), C.rotation.y = Qe + d + (le ? 0 : v), C.rotation.z = w + u;
        const I = le ? xt + 0.22 : xt + 0.35, z = xt - 0.22;
        C.rotation.x = Math.max(z, Math.min(I, C.rotation.x)), C.rotation.y = Math.max(Qe - 0.65, Math.min(Qe + 0.65, C.rotation.y)), C.rotation.z = Math.max(w - 0.12, Math.min(w + 0.12, C.rotation.z));
      }
    }
    (!Y || le) && tt && tt !== Je && tt !== C && (tt.rotation.x = Math.max(_t - 0.2, Math.min(_t + 0.35, tt.rotation.x)));
    let yi = 0, Vi = 0, Hi = 0, wn = 0, fs = 0;
    switch (me) {
      case "sleeping":
        Vi = 0.6;
        break;
      case "listening":
        break;
      case "thinking":
        break;
      case "speaking":
      case "speaking_alt":
        Le && (yi = 0.22);
        break;
      case "angry":
        Hi = 0.7;
        break;
      case "error":
        wn = 0.6;
        break;
    }
    const Aa = yi === 0 ? 12 : 6, Ra = Vi === 0 ? 12 : 6;
    if (s = it(s, yi, Xe, Aa), a = it(a, Vi, Xe, Ra), o = it(o, Hi, Xe, 6), l = it(l, wn, Xe, 6), c = it(c, fs, Xe, 6), (!Mi || !Le) && (s = 0), me !== "sleeping" && (a = 0), (me === "thinking" || me === "listening" || Mi) && (c = 0), s < 5e-3 && (s = 0), a < 5e-3 && (a = 0), r.expressionManager?.setValue("happy", s), r.expressionManager?.setValue("relaxed", a), r.expressionManager?.setValue("angry", o), r.expressionManager?.setValue("sad", l), r.expressionManager?.setValue("surprised", c), me === "sleeping") n = it(n, 1, Xe, 6);
    else {
      t > i + 0.15 && (i = t + 3 + e() * 3);
      const M = t < i ? 0 : Math.sin(Math.PI * (t - i) / 0.15);
      n = it(n, Math.max(0, M), Xe, 20);
    }
    r.expressionManager?.setValue("blink", n);
    const ms = Le && Number.isFinite(J) ? Math.max(0, J) : 0, xr = me === "sleeping" ? 0 : Math.min(0.75, ms), An = xr * 0.2;
    if (r.expressionManager?.setValue("aa", xr), r.expressionManager?.setValue("oh", An), Ne && Ut !== void 0) {
      const M = me === "sleeping" ? "sleeping" : Le ? xr === 0 ? "deadband" : "open" : "audio_inactive";
      try {
        Ne(Xt, Ut, J, Le, me, ms, xr, xr, An, M);
      } catch {
      }
    }
    if (r.lookAt && E) {
      r.humanoid?.update?.(), r.scene.updateMatrixWorld(!0), r.lookAt.getLookAtWorldPosition(S), r.lookAt.getLookAtWorldQuaternion(T), r.lookAt.getFaceFrontQuaternion(A);
      const M = Bi ? 0 : 1;
      x.set(p * 0.35 * M, f * 0.25 * M, 1.5).applyQuaternion(A).applyQuaternion(T), g.position.copy(S).add(x), r.scene.worldToLocal(g.position);
    }
    r.update(Xe);
  };
  return _e.isGreeting = () => je, _e.playCustomAnimation = async (ae, J, me) => {
    const pe = ++b;
    if (G(), ke = !1, $) return 0;
    try {
      if (!ae) return ue && (ue.fadeOut(0.4), ue = null), V && !le && (V.reset().setEffectiveWeight(1).fadeIn(0.4).play(), ee = t), 0;
      let Le = He.get(ae);
      if (!Le) {
        if (Le = await Oh(ae, r) ?? void 0, $ || pe !== b) return 0;
        Le && He.set(ae, Le);
      }
      if (!Le) return J?.(), 0;
      ue && (ue.stop(), ue = null);
      const Ne = te.clipAction(Le);
      return qi(ae) ? (Ne === V ? (Ne.setEffectiveWeight(1), Ne.isRunning() || Ne.play()) : (Ne.reset().setLoop(Kn, 1 / 0), Ne.clampWhenFinished = !1, Ne.setEffectiveWeight(1).fadeIn(0.2).play(), V && V !== Ne && V.fadeOut(0.2)), V = Ne, K = ae, ee = t, Ue = kn(ae, Le.duration), Y = !0, q = J ?? null, B = setTimeout(Z, 4e3), Le.duration) : (Ne.reset().setLoop(sn, 1), Ne.clampWhenFinished = !0, Ne.setEffectiveWeight(1).fadeIn(0.2).play(), V && V !== Ne && V.fadeOut(0.2), ue = Ne, Ye = Math.max(1, Le.duration), Y = !0, me?.hold ? (ke = !0, Ye) : (q = () => {
        J?.();
      }, B = setTimeout(() => {
        vt(), Z();
      }, (Ye + 1.2) * 1e3), Ye));
    } catch {
      return $ || pe !== b || J?.(), 0;
    }
  }, _e.dispose = () => {
    $ = !0, ++b, G(), te.removeEventListener("finished", dt), te.stopAllAction(), te.uncacheRoot(X), r.lookAt && r.lookAt.target === g && (r.lookAt.target = null), g.removeFromParent();
  }, _e;
}
const OM = 2;
function FM(r, e, t) {
  return r - e >= 1e3 / t - OM;
}
var Gs = (r, e, t) => new Promise((i, n) => {
  var s = (l) => {
    try {
      o(t.next(l));
    } catch (c) {
      n(c);
    }
  }, a = (l) => {
    try {
      o(t.throw(l));
    } catch (c) {
      n(c);
    }
  }, o = (l) => l.done ? i(l.value) : Promise.resolve(l.value).then(s, a);
  o((t = t.apply(r, e)).next());
}), rt = (r, e, t) => new Promise((i, n) => {
  var s = (l) => {
    try {
      o(t.next(l));
    } catch (c) {
      n(c);
    }
  }, a = (l) => {
    try {
      o(t.throw(l));
    } catch (c) {
      n(c);
    }
  }, o = (l) => l.done ? i(l.value) : Promise.resolve(l.value).then(s, a);
  o((t = t.apply(r, e)).next());
}), Bh = class extends at {
  constructor(r) {
    super(), this.weight = 0, this.isBinary = !1, this.overrideBlink = "none", this.overrideLookAt = "none", this.overrideMouth = "none", this._binds = [], this.name = `VRMExpression_${r}`, this.expressionName = r, this.type = "VRMExpression", this.visible = !1;
  }
  get binds() {
    return this._binds;
  }
  get overrideBlinkAmount() {
    return this.overrideBlink === "block" ? 0 < this.outputWeight ? 1 : 0 : this.overrideBlink === "blend" ? this.outputWeight : 0;
  }
  get overrideLookAtAmount() {
    return this.overrideLookAt === "block" ? 0 < this.outputWeight ? 1 : 0 : this.overrideLookAt === "blend" ? this.outputWeight : 0;
  }
  get overrideMouthAmount() {
    return this.overrideMouth === "block" ? 0 < this.outputWeight ? 1 : 0 : this.overrideMouth === "blend" ? this.outputWeight : 0;
  }
  get outputWeight() {
    return this.isBinary ? this.weight > 0.5 ? 1 : 0 : this.weight;
  }
  addBind(r) {
    this._binds.push(r);
  }
  deleteBind(r) {
    const e = this._binds.indexOf(r);
    e >= 0 && this._binds.splice(e, 1);
  }
  applyWeight(r) {
    var e;
    let t = this.outputWeight;
    t *= (e = r?.multiplier) != null ? e : 1, this.isBinary && t < 1 && (t = 0), this._binds.forEach((i) => i.applyWeight(t));
  }
  clearAppliedWeight() {
    this._binds.forEach((r) => r.clearAppliedWeight());
  }
};
function Ad(r, e, t) {
  var i, n;
  const s = r.parser.json, a = (i = s.nodes) == null ? void 0 : i[e];
  if (a == null) return console.warn(`extractPrimitivesInternal: Attempt to use nodes[${e}] of glTF but the node doesn't exist`), null;
  const o = a.mesh;
  if (o == null) return null;
  const l = (n = s.meshes) == null ? void 0 : n[o];
  if (l == null) return console.warn(`extractPrimitivesInternal: Attempt to use meshes[${o}] of glTF but the mesh doesn't exist`), null;
  const c = l.primitives.length, h = [];
  return t.traverse((d) => {
    h.length < c && d.isMesh && h.push(d);
  }), h;
}
function Vh(r, e) {
  return rt(this, null, function* () {
    const t = yield r.parser.getDependency("node", e);
    return Ad(r, e, t);
  });
}
function Hh(r) {
  return rt(this, null, function* () {
    const e = yield r.parser.getDependencies("node"), t = /* @__PURE__ */ new Map();
    return e.forEach((i, n) => {
      const s = Ad(r, n, i);
      s != null && t.set(n, s);
    }), t;
  });
}
var Al = { Aa: "aa", Ih: "ih", Ou: "ou", Ee: "ee", Oh: "oh", Blink: "blink", Happy: "happy", Angry: "angry", Sad: "sad", Relaxed: "relaxed", LookUp: "lookUp", Surprised: "surprised", LookDown: "lookDown", LookLeft: "lookLeft", LookRight: "lookRight", BlinkLeft: "blinkLeft", BlinkRight: "blinkRight", Neutral: "neutral" };
function Rd(r) {
  return Math.max(Math.min(r, 1), 0);
}
var kh = class Pd {
  constructor() {
    this.blinkExpressionNames = ["blink", "blinkLeft", "blinkRight"], this.lookAtExpressionNames = ["lookLeft", "lookRight", "lookUp", "lookDown"], this.mouthExpressionNames = ["aa", "ee", "ih", "oh", "ou"], this._expressions = [], this._expressionMap = {};
  }
  get expressions() {
    return this._expressions.concat();
  }
  get expressionMap() {
    return Object.assign({}, this._expressionMap);
  }
  get presetExpressionMap() {
    const e = {}, t = new Set(Object.values(Al));
    return Object.entries(this._expressionMap).forEach(([i, n]) => {
      t.has(i) && (e[i] = n);
    }), e;
  }
  get customExpressionMap() {
    const e = {}, t = new Set(Object.values(Al));
    return Object.entries(this._expressionMap).forEach(([i, n]) => {
      t.has(i) || (e[i] = n);
    }), e;
  }
  copy(e) {
    return this._expressions.concat().forEach((t) => {
      this.unregisterExpression(t);
    }), e._expressions.forEach((t) => {
      this.registerExpression(t);
    }), this.blinkExpressionNames = e.blinkExpressionNames.concat(), this.lookAtExpressionNames = e.lookAtExpressionNames.concat(), this.mouthExpressionNames = e.mouthExpressionNames.concat(), this;
  }
  clone() {
    return new Pd().copy(this);
  }
  getExpression(e) {
    var t;
    return (t = this._expressionMap[e]) != null ? t : null;
  }
  registerExpression(e) {
    this._expressions.push(e), this._expressionMap[e.expressionName] = e;
  }
  unregisterExpression(e) {
    const t = this._expressions.indexOf(e);
    t === -1 && console.warn("VRMExpressionManager: The specified expressions is not registered"), this._expressions.splice(t, 1), delete this._expressionMap[e.expressionName];
  }
  getValue(e) {
    var t;
    return (t = this.getExpression(e)?.weight) != null ? t : null;
  }
  setValue(e, t) {
    const i = this.getExpression(e);
    i && (i.weight = Rd(t));
  }
  resetValues() {
    this._expressions.forEach((e) => {
      e.weight = 0;
    });
  }
  getExpressionTrackName(e) {
    const t = this.getExpression(e);
    return t ? `${t.name}.weight` : null;
  }
  update() {
    const e = this._calculateWeightMultipliers();
    this._expressions.forEach((t) => {
      t.clearAppliedWeight();
    }), this._expressions.forEach((t) => {
      let i = 1;
      const n = t.expressionName;
      this.blinkExpressionNames.indexOf(n) !== -1 && (i *= e.blink), this.lookAtExpressionNames.indexOf(n) !== -1 && (i *= e.lookAt), this.mouthExpressionNames.indexOf(n) !== -1 && (i *= e.mouth), t.applyWeight({ multiplier: i });
    });
  }
  _calculateWeightMultipliers() {
    let e = 1, t = 1, i = 1;
    return this._expressions.forEach((n) => {
      e -= n.overrideBlinkAmount, t -= n.overrideLookAtAmount, i -= n.overrideMouthAmount;
    }), e = Math.max(0, e), t = Math.max(0, t), i = Math.max(0, i), { blink: e, lookAt: t, mouth: i };
  }
}, zn = { Color: "color", EmissionColor: "emissionColor", ShadeColor: "shadeColor", RimColor: "rimColor", OutlineColor: "outlineColor" }, BM = { _Color: zn.Color, _EmissionColor: zn.EmissionColor, _ShadeColor: zn.ShadeColor, _RimColor: zn.RimColor, _OutlineColor: zn.OutlineColor }, VM = new be(), Cd = class Ld {
  constructor({ material: e, type: t, targetValue: i, targetAlpha: n }) {
    this.material = e, this.type = t, this.targetValue = i, this.targetAlpha = n ?? 1;
    const s = this._initColorBindState(), a = this._initAlphaBindState();
    this._state = { color: s, alpha: a };
  }
  applyWeight(e) {
    const { color: t, alpha: i } = this._state;
    if (t != null) {
      const { propertyName: n, deltaValue: s } = t;
      this.material[n]?.add(VM.copy(s).multiplyScalar(e));
    }
    if (i != null) {
      const { propertyName: n, deltaValue: s } = i;
      this.material[n] != null && (this.material[n] += s * e);
    }
  }
  clearAppliedWeight() {
    const { color: e, alpha: t } = this._state;
    if (e != null) {
      const { propertyName: i, initialValue: n } = e;
      this.material[i]?.copy(n);
    }
    if (t != null) {
      const { propertyName: i, initialValue: n } = t;
      this.material[i] != null && (this.material[i] = n);
    }
  }
  _initColorBindState() {
    var e, t, i;
    const { material: n, type: s, targetValue: a } = this, o = this._getPropertyNameMap(), l = (t = (e = o?.[s]) == null ? void 0 : e[0]) != null ? t : null;
    if (l == null) return console.warn(`Tried to add a material color bind to the material ${(i = n.name) != null ? i : "(no name)"}, the type ${s} but the material or the type is not supported.`), null;
    const c = n[l].clone(), h = new be(a.r - c.r, a.g - c.g, a.b - c.b);
    return { propertyName: l, initialValue: c, deltaValue: h };
  }
  _initAlphaBindState() {
    var e, t, i;
    const { material: n, type: s, targetAlpha: a } = this, o = this._getPropertyNameMap(), l = (t = (e = o?.[s]) == null ? void 0 : e[1]) != null ? t : null;
    if (l == null && a !== 1) return console.warn(`Tried to add a material alpha bind to the material ${(i = n.name) != null ? i : "(no name)"}, the type ${s} but the material or the type does not support alpha.`), null;
    if (l == null) return null;
    const c = n[l], h = a - c;
    return { propertyName: l, initialValue: c, deltaValue: h };
  }
  _getPropertyNameMap() {
    var e, t;
    return (t = (e = Object.entries(Ld._propertyNameMapMap).find(([i]) => this.material[i] === !0)) == null ? void 0 : e[1]) != null ? t : null;
  }
};
Cd._propertyNameMapMap = { isMeshStandardMaterial: { color: ["color", "opacity"], emissionColor: ["emissive", null] }, isMeshBasicMaterial: { color: ["color", "opacity"] }, isMToonMaterial: { color: ["color", "opacity"], emissionColor: ["emissive", null], outlineColor: ["outlineColorFactor", null], matcapColor: ["matcapFactor", null], rimColor: ["parametricRimColorFactor", null], shadeColor: ["shadeColorFactor", null] } };
var zh = Cd, ma = class {
  constructor({ primitives: r, index: e, weight: t }) {
    this.primitives = r, this.index = e, this.weight = t;
  }
  applyWeight(r) {
    this.primitives.forEach((e) => {
      var t;
      ((t = e.morphTargetInfluences) == null ? void 0 : t[this.index]) != null && (e.morphTargetInfluences[this.index] += this.weight * r);
    });
  }
  clearAppliedWeight() {
    this.primitives.forEach((r) => {
      var e;
      ((e = r.morphTargetInfluences) == null ? void 0 : e[this.index]) != null && (r.morphTargetInfluences[this.index] = 0);
    });
  }
}, Gh = new We(), Id = class Nd {
  constructor({ material: e, scale: t, offset: i }) {
    var n, s;
    this.material = e, this.scale = t, this.offset = i;
    const a = (n = Object.entries(Nd._propertyNamesMap).find(([o]) => e[o] === !0)) == null ? void 0 : n[1];
    a == null ? (console.warn(`Tried to add a texture transform bind to the material ${(s = e.name) != null ? s : "(no name)"} but the material is not supported.`), this._properties = []) : (this._properties = [], a.forEach((o) => {
      var l;
      const c = (l = e[o]) == null ? void 0 : l.clone();
      if (!c) return null;
      e[o] = c;
      const h = c.offset.clone(), d = c.repeat.clone(), u = i.clone().sub(h), p = t.clone().sub(d);
      this._properties.push({ name: o, initialOffset: h, deltaOffset: u, initialScale: d, deltaScale: p });
    }));
  }
  applyWeight(e) {
    this._properties.forEach((t) => {
      const i = this.material[t.name];
      i !== void 0 && (i.offset.add(Gh.copy(t.deltaOffset).multiplyScalar(e)), i.repeat.add(Gh.copy(t.deltaScale).multiplyScalar(e)));
    });
  }
  clearAppliedWeight() {
    this._properties.forEach((e) => {
      const t = this.material[e.name];
      t !== void 0 && (t.offset.copy(e.initialOffset), t.repeat.copy(e.initialScale));
    });
  }
};
Id._propertyNamesMap = { isMeshStandardMaterial: ["map", "emissiveMap", "bumpMap", "normalMap", "displacementMap", "roughnessMap", "metalnessMap", "alphaMap"], isMeshBasicMaterial: ["map", "specularMap", "alphaMap"], isMToonMaterial: ["map", "normalMap", "emissiveMap", "shadeMultiplyTexture", "rimMultiplyTexture", "outlineWidthMultiplyTexture", "uvAnimationMaskTexture"] };
var Wh = Id, HM = /* @__PURE__ */ new Set(["1.0", "1.0-beta"]), Ud = class Dd {
  get name() {
    return "VRMExpressionLoaderPlugin";
  }
  constructor(e) {
    this.parser = e;
  }
  afterRoot(e) {
    return rt(this, null, function* () {
      e.userData.vrmExpressionManager = yield this._import(e);
    });
  }
  _import(e) {
    return rt(this, null, function* () {
      const t = yield this._v1Import(e);
      return t || (yield this._v0Import(e)) || null;
    });
  }
  _v1Import(e) {
    return rt(this, null, function* () {
      var t, i;
      const n = this.parser.json;
      if (((t = n.extensionsUsed) == null ? void 0 : t.indexOf("VRMC_vrm")) === -1) return null;
      const s = (i = n.extensions) == null ? void 0 : i.VRMC_vrm;
      if (!s) return null;
      const a = s.specVersion;
      if (!HM.has(a)) return console.warn(`VRMExpressionLoaderPlugin: Unknown VRMC_vrm specVersion "${a}"`), null;
      const o = s.expressions;
      if (!o) return null;
      const l = new Set(Object.values(Al)), c = /* @__PURE__ */ new Map();
      o.preset != null && Object.entries(o.preset).forEach(([d, u]) => {
        if (u != null) {
          if (!l.has(d)) {
            console.warn(`VRMExpressionLoaderPlugin: Unknown preset name "${d}" detected. Ignoring the expression`);
            return;
          }
          c.set(d, u);
        }
      }), o.custom != null && Object.entries(o.custom).forEach(([d, u]) => {
        if (l.has(d)) {
          console.warn(`VRMExpressionLoaderPlugin: Custom expression cannot have preset name "${d}". Ignoring the expression`);
          return;
        }
        c.set(d, u);
      });
      const h = new kh();
      return yield Promise.all(Array.from(c.entries()).map((d) => rt(this, [d], function* ([u, p]) {
        var f, v, m, g, E, R, y;
        const S = new Bh(u);
        if (e.scene.add(S), S.isBinary = (f = p.isBinary) != null ? f : !1, S.overrideBlink = (v = p.overrideBlink) != null ? v : "none", S.overrideLookAt = (m = p.overrideLookAt) != null ? m : "none", S.overrideMouth = (g = p.overrideMouth) != null ? g : "none", (E = p.morphTargetBinds) == null || E.forEach((T) => rt(this, null, function* () {
          var A;
          if (T.node === void 0 || T.index === void 0) return;
          const x = yield Vh(e, T.node), b = T.index;
          if (!x.every((N) => Array.isArray(N.morphTargetInfluences) && b < N.morphTargetInfluences.length)) {
            console.warn(`VRMExpressionLoaderPlugin: ${p.name} attempts to index morph #${b} but not found.`);
            return;
          }
          S.addBind(new ma({ primitives: x, index: b, weight: (A = T.weight) != null ? A : 1 }));
        })), p.materialColorBinds || p.textureTransformBinds) {
          const T = [];
          e.scene.traverse((A) => {
            const x = A.material;
            x && (Array.isArray(x) ? T.push(...x) : T.push(x));
          }), (R = p.materialColorBinds) == null || R.forEach((A) => rt(this, null, function* () {
            T.filter((x) => {
              var b;
              const N = (b = this.parser.associations.get(x)) == null ? void 0 : b.materials;
              return A.material === N;
            }).forEach((x) => {
              S.addBind(new zh({ material: x, type: A.type, targetValue: new be().fromArray(A.targetValue), targetAlpha: A.targetValue[3] }));
            });
          })), (y = p.textureTransformBinds) == null || y.forEach((A) => rt(this, null, function* () {
            T.filter((x) => {
              var b;
              const N = (b = this.parser.associations.get(x)) == null ? void 0 : b.materials;
              return A.material === N;
            }).forEach((x) => {
              var b, N;
              S.addBind(new Wh({ material: x, offset: new We().fromArray((b = A.offset) != null ? b : [0, 0]), scale: new We().fromArray((N = A.scale) != null ? N : [1, 1]) }));
            });
          }));
        }
        h.registerExpression(S);
      }))), h;
    });
  }
  _v0Import(e) {
    return rt(this, null, function* () {
      var t;
      const i = this.parser.json, n = (t = i.extensions) == null ? void 0 : t.VRM;
      if (!n) return null;
      const s = n.blendShapeMaster;
      if (!s) return null;
      const a = new kh(), o = s.blendShapeGroups;
      if (!o) return a;
      const l = /* @__PURE__ */ new Set();
      return yield Promise.all(o.map((c) => rt(this, null, function* () {
        var h;
        const d = c.presetName, u = d != null && Dd.v0v1PresetNameMap[d] || null, p = u ?? c.name;
        if (p == null) {
          console.warn("VRMExpressionLoaderPlugin: One of custom expressions has no name. Ignoring the expression");
          return;
        }
        if (l.has(p)) {
          console.warn(`VRMExpressionLoaderPlugin: An expression preset ${d} has duplicated entries. Ignoring the expression`);
          return;
        }
        l.add(p);
        const f = new Bh(p);
        e.scene.add(f), f.isBinary = (h = c.isBinary) != null ? h : !1, c.binds && c.binds.forEach((m) => rt(this, null, function* () {
          var g;
          if (m.mesh === void 0 || m.index === void 0) return;
          const E = [];
          if ((g = i.nodes) == null || g.forEach((y, S) => {
            y.mesh === m.mesh && E.push(S);
          }), E.length === 0) {
            console.warn(`VRMExpressionLoaderPlugin: ${c.name} attempts to bind a morph target to the mesh #${m.mesh} but the mesh is not found or not used in the scene. Ignoring the bind.`);
            return;
          }
          const R = m.index;
          yield Promise.all(E.map((y) => rt(this, null, function* () {
            var S;
            const T = yield Vh(e, y);
            if (!T.every((A) => Array.isArray(A.morphTargetInfluences) && R < A.morphTargetInfluences.length)) {
              console.warn(`VRMExpressionLoaderPlugin: ${c.name} attempts to index ${R}th morph but not found.`);
              return;
            }
            f.addBind(new ma({ primitives: T, index: R, weight: 0.01 * ((S = m.weight) != null ? S : 100) }));
          })));
        }));
        const v = c.materialValues;
        v && v.length !== 0 && v.forEach((m) => {
          if (m.materialName === void 0 || m.propertyName === void 0 || m.targetValue === void 0) return;
          const g = [];
          e.scene.traverse((R) => {
            if (R.material) {
              const y = R.material;
              Array.isArray(y) ? g.push(...y.filter((S) => (S.name === m.materialName || S.name === m.materialName + " (Outline)") && g.indexOf(S) === -1)) : y.name === m.materialName && g.indexOf(y) === -1 && g.push(y);
            }
          });
          const E = m.propertyName;
          g.forEach((R) => {
            if (E === "_MainTex_ST") {
              const S = new We(m.targetValue[0], m.targetValue[1]), T = new We(m.targetValue[2], m.targetValue[3]);
              T.y = 1 - T.y - S.y, f.addBind(new Wh({ material: R, scale: S, offset: T }));
              return;
            }
            const y = BM[E];
            if (y) {
              f.addBind(new zh({ material: R, type: y, targetValue: new be().fromArray(m.targetValue), targetAlpha: m.targetValue[3] }));
              return;
            }
            console.warn(E + " is not supported");
          });
        }), a.registerExpression(f);
      }))), a;
    });
  }
};
Ud.v0v1PresetNameMap = { a: "aa", e: "ee", i: "ih", o: "oh", u: "ou", blink: "blink", joy: "happy", angry: "angry", sorrow: "sad", fun: "relaxed", lookup: "lookUp", lookdown: "lookDown", lookleft: "lookLeft", lookright: "lookRight", blink_l: "blinkLeft", blink_r: "blinkRight", neutral: "neutral" };
var kM = Ud, Rl = class ln {
  constructor(e, t) {
    this._firstPersonOnlyLayer = ln.DEFAULT_FIRSTPERSON_ONLY_LAYER, this._thirdPersonOnlyLayer = ln.DEFAULT_THIRDPERSON_ONLY_LAYER, this._initializedLayers = !1, this.humanoid = e, this.meshAnnotations = t;
  }
  copy(e) {
    if (this.humanoid !== e.humanoid) throw new Error("VRMFirstPerson: humanoid must be same in order to copy");
    return this.meshAnnotations = e.meshAnnotations.map((t) => ({ meshes: t.meshes.concat(), type: t.type })), this;
  }
  clone() {
    return new ln(this.humanoid, this.meshAnnotations).copy(this);
  }
  get firstPersonOnlyLayer() {
    return this._firstPersonOnlyLayer;
  }
  get thirdPersonOnlyLayer() {
    return this._thirdPersonOnlyLayer;
  }
  setup({ firstPersonOnlyLayer: e = ln.DEFAULT_FIRSTPERSON_ONLY_LAYER, thirdPersonOnlyLayer: t = ln.DEFAULT_THIRDPERSON_ONLY_LAYER } = {}) {
    this._initializedLayers || (this._firstPersonOnlyLayer = e, this._thirdPersonOnlyLayer = t, this.meshAnnotations.forEach((i) => {
      i.meshes.forEach((n) => {
        i.type === "firstPersonOnly" ? (n.layers.set(this._firstPersonOnlyLayer), n.traverse((s) => s.layers.set(this._firstPersonOnlyLayer))) : i.type === "thirdPersonOnly" ? (n.layers.set(this._thirdPersonOnlyLayer), n.traverse((s) => s.layers.set(this._thirdPersonOnlyLayer))) : i.type === "auto" && this._createHeadlessModel(n);
      });
    }), this._initializedLayers = !0);
  }
  _excludeTriangles(e, t, i, n) {
    let s = 0;
    if (t != null && t.length > 0) for (let a = 0; a < e.length; a += 3) {
      const o = e[a], l = e[a + 1], c = e[a + 2], h = t[o], d = i[o];
      if (h[0] > 0 && n.includes(d[0]) || h[1] > 0 && n.includes(d[1]) || h[2] > 0 && n.includes(d[2]) || h[3] > 0 && n.includes(d[3])) continue;
      const u = t[l], p = i[l];
      if (u[0] > 0 && n.includes(p[0]) || u[1] > 0 && n.includes(p[1]) || u[2] > 0 && n.includes(p[2]) || u[3] > 0 && n.includes(p[3])) continue;
      const f = t[c], v = i[c];
      f[0] > 0 && n.includes(v[0]) || f[1] > 0 && n.includes(v[1]) || f[2] > 0 && n.includes(v[2]) || f[3] > 0 && n.includes(v[3]) || (e[s++] = o, e[s++] = l, e[s++] = c);
    }
    return s;
  }
  _createErasedMesh(e, t) {
    const i = new Ju(e.geometry.clone(), e.material);
    i.name = `${e.name}(erase)`, i.frustumCulled = e.frustumCulled, i.layers.set(this._firstPersonOnlyLayer);
    const n = i.geometry, s = n.getAttribute("skinIndex"), a = s instanceof Qc ? [] : s.array, o = [];
    for (let v = 0; v < a.length; v += 4) o.push([a[v], a[v + 1], a[v + 2], a[v + 3]]);
    const l = n.getAttribute("skinWeight"), c = l instanceof Qc ? [] : l.array, h = [];
    for (let v = 0; v < c.length; v += 4) h.push([c[v], c[v + 1], c[v + 2], c[v + 3]]);
    const d = n.getIndex();
    if (!d) throw new Error("The geometry doesn't have an index buffer");
    const u = Array.from(d.array), p = this._excludeTriangles(u, h, o, t), f = [];
    for (let v = 0; v < p; v++) f[v] = u[v];
    return n.setIndex(f), e.onBeforeRender && (i.onBeforeRender = e.onBeforeRender), i.bind(new Sn(e.skeleton.bones, e.skeleton.boneInverses), new Fe()), i;
  }
  _createHeadlessModelForSkinnedMesh(e, t) {
    const i = [];
    if (t.skeleton.bones.forEach((s, a) => {
      this._isEraseTarget(s) && i.push(a);
    }), !i.length) {
      t.layers.enable(this._thirdPersonOnlyLayer), t.layers.enable(this._firstPersonOnlyLayer);
      return;
    }
    t.layers.set(this._thirdPersonOnlyLayer);
    const n = this._createErasedMesh(t, i);
    e.add(n);
  }
  _createHeadlessModel(e) {
    if (e.type === "Group") if (e.layers.set(this._thirdPersonOnlyLayer), this._isEraseTarget(e)) e.traverse((t) => t.layers.set(this._thirdPersonOnlyLayer));
    else {
      const t = new Qt();
      t.name = `_headless_${e.name}`, t.layers.set(this._firstPersonOnlyLayer), e.parent.add(t), e.children.filter((i) => i.type === "SkinnedMesh").forEach((i) => {
        const n = i;
        this._createHeadlessModelForSkinnedMesh(t, n);
      });
    }
    else if (e.type === "SkinnedMesh") {
      const t = e;
      this._createHeadlessModelForSkinnedMesh(e.parent, t);
    } else this._isEraseTarget(e) && (e.layers.set(this._thirdPersonOnlyLayer), e.traverse((t) => t.layers.set(this._thirdPersonOnlyLayer)));
  }
  _isEraseTarget(e) {
    return e === this.humanoid.getRawBoneNode("head") ? !0 : e.parent ? this._isEraseTarget(e.parent) : !1;
  }
};
Rl.DEFAULT_FIRSTPERSON_ONLY_LAYER = 9, Rl.DEFAULT_THIRDPERSON_ONLY_LAYER = 10;
var Xh = Rl, zM = /* @__PURE__ */ new Set(["1.0", "1.0-beta"]), GM = class {
  get name() {
    return "VRMFirstPersonLoaderPlugin";
  }
  constructor(r) {
    this.parser = r;
  }
  afterRoot(r) {
    return rt(this, null, function* () {
      const e = r.userData.vrmHumanoid;
      if (e !== null) {
        if (e === void 0) throw new Error("VRMFirstPersonLoaderPlugin: vrmHumanoid is undefined. VRMHumanoidLoaderPlugin have to be used first");
        r.userData.vrmFirstPerson = yield this._import(r, e);
      }
    });
  }
  _import(r, e) {
    return rt(this, null, function* () {
      if (e == null) return null;
      const t = yield this._v1Import(r, e);
      return t || (yield this._v0Import(r, e)) || null;
    });
  }
  _v1Import(r, e) {
    return rt(this, null, function* () {
      var t, i;
      const n = this.parser.json;
      if (((t = n.extensionsUsed) == null ? void 0 : t.indexOf("VRMC_vrm")) === -1) return null;
      const s = (i = n.extensions) == null ? void 0 : i.VRMC_vrm;
      if (!s) return null;
      const a = s.specVersion;
      if (!zM.has(a)) return console.warn(`VRMFirstPersonLoaderPlugin: Unknown VRMC_vrm specVersion "${a}"`), null;
      const o = s.firstPerson, l = [], c = yield Hh(r);
      return Array.from(c.entries()).forEach(([h, d]) => {
        var u, p;
        const f = (u = o?.meshAnnotations) == null ? void 0 : u.find((v) => v.node === h);
        l.push({ meshes: d, type: (p = f?.type) != null ? p : "auto" });
      }), new Xh(e, l);
    });
  }
  _v0Import(r, e) {
    return rt(this, null, function* () {
      var t;
      const i = this.parser.json, n = (t = i.extensions) == null ? void 0 : t.VRM;
      if (!n) return null;
      const s = n.firstPerson;
      if (!s) return null;
      const a = [], o = yield Hh(r);
      return Array.from(o.entries()).forEach(([l, c]) => {
        const h = i.nodes[l], d = s.meshAnnotations ? s.meshAnnotations.find((u) => u.mesh === h.mesh) : void 0;
        a.push({ meshes: c, type: this._convertV0FlagToV1Type(d?.firstPersonFlag) });
      }), new Xh(e, a);
    });
  }
  _convertV0FlagToV1Type(r) {
    return r === "FirstPersonOnly" ? "firstPersonOnly" : r === "ThirdPersonOnly" ? "thirdPersonOnly" : r === "Both" ? "both" : "auto";
  }
}, jh = new P(), qh = new P(), WM = new Ce(), Yh = class extends Qt {
  constructor(r) {
    super(), this.vrmHumanoid = r, this._boneAxesMap = /* @__PURE__ */ new Map(), Object.values(r.humanBones).forEach((e) => {
      const t = new Cm(1);
      t.matrixAutoUpdate = !1, t.material.depthTest = !1, t.material.depthWrite = !1, this.add(t), this._boneAxesMap.set(e, t);
    });
  }
  dispose() {
    Array.from(this._boneAxesMap.values()).forEach((r) => {
      r.geometry.dispose(), r.material.dispose();
    });
  }
  updateMatrixWorld(r) {
    Array.from(this._boneAxesMap.entries()).forEach(([e, t]) => {
      e.node.updateWorldMatrix(!0, !1), e.node.matrixWorld.decompose(jh, WM, qh);
      const i = jh.set(0.1, 0.1, 0.1).divide(qh);
      t.matrix.copy(e.node.matrixWorld).scale(i);
    }), super.updateMatrixWorld(r);
  }
}, Eo = ["hips", "spine", "chest", "upperChest", "neck", "head", "leftEye", "rightEye", "jaw", "leftUpperLeg", "leftLowerLeg", "leftFoot", "leftToes", "rightUpperLeg", "rightLowerLeg", "rightFoot", "rightToes", "leftShoulder", "leftUpperArm", "leftLowerArm", "leftHand", "rightShoulder", "rightUpperArm", "rightLowerArm", "rightHand", "leftThumbMetacarpal", "leftThumbProximal", "leftThumbDistal", "leftIndexProximal", "leftIndexIntermediate", "leftIndexDistal", "leftMiddleProximal", "leftMiddleIntermediate", "leftMiddleDistal", "leftRingProximal", "leftRingIntermediate", "leftRingDistal", "leftLittleProximal", "leftLittleIntermediate", "leftLittleDistal", "rightThumbMetacarpal", "rightThumbProximal", "rightThumbDistal", "rightIndexProximal", "rightIndexIntermediate", "rightIndexDistal", "rightMiddleProximal", "rightMiddleIntermediate", "rightMiddleDistal", "rightRingProximal", "rightRingIntermediate", "rightRingDistal", "rightLittleProximal", "rightLittleIntermediate", "rightLittleDistal"], XM = { hips: null, spine: "hips", chest: "spine", upperChest: "chest", neck: "upperChest", head: "neck", leftEye: "head", rightEye: "head", jaw: "head", leftUpperLeg: "hips", leftLowerLeg: "leftUpperLeg", leftFoot: "leftLowerLeg", leftToes: "leftFoot", rightUpperLeg: "hips", rightLowerLeg: "rightUpperLeg", rightFoot: "rightLowerLeg", rightToes: "rightFoot", leftShoulder: "upperChest", leftUpperArm: "leftShoulder", leftLowerArm: "leftUpperArm", leftHand: "leftLowerArm", rightShoulder: "upperChest", rightUpperArm: "rightShoulder", rightLowerArm: "rightUpperArm", rightHand: "rightLowerArm", leftThumbMetacarpal: "leftHand", leftThumbProximal: "leftThumbMetacarpal", leftThumbDistal: "leftThumbProximal", leftIndexProximal: "leftHand", leftIndexIntermediate: "leftIndexProximal", leftIndexDistal: "leftIndexIntermediate", leftMiddleProximal: "leftHand", leftMiddleIntermediate: "leftMiddleProximal", leftMiddleDistal: "leftMiddleIntermediate", leftRingProximal: "leftHand", leftRingIntermediate: "leftRingProximal", leftRingDistal: "leftRingIntermediate", leftLittleProximal: "leftHand", leftLittleIntermediate: "leftLittleProximal", leftLittleDistal: "leftLittleIntermediate", rightThumbMetacarpal: "rightHand", rightThumbProximal: "rightThumbMetacarpal", rightThumbDistal: "rightThumbProximal", rightIndexProximal: "rightHand", rightIndexIntermediate: "rightIndexProximal", rightIndexDistal: "rightIndexIntermediate", rightMiddleProximal: "rightHand", rightMiddleIntermediate: "rightMiddleProximal", rightMiddleDistal: "rightMiddleIntermediate", rightRingProximal: "rightHand", rightRingIntermediate: "rightRingProximal", rightRingDistal: "rightRingIntermediate", rightLittleProximal: "rightHand", rightLittleIntermediate: "rightLittleProximal", rightLittleDistal: "rightLittleIntermediate" };
function Od(r) {
  return r.invert ? r.invert() : r.inverse(), r;
}
var Ar = new P(), Rr = new Ce(), Pl = class {
  constructor(r) {
    this.humanBones = r, this.restPose = this.getAbsolutePose();
  }
  getAbsolutePose() {
    const r = {};
    return Object.keys(this.humanBones).forEach((e) => {
      const t = e, i = this.getBoneNode(t);
      i && (Ar.copy(i.position), Rr.copy(i.quaternion), r[t] = { position: Ar.toArray(), rotation: Rr.toArray() });
    }), r;
  }
  getPose() {
    const r = {};
    return Object.keys(this.humanBones).forEach((e) => {
      const t = e, i = this.getBoneNode(t);
      if (!i) return;
      Ar.set(0, 0, 0), Rr.identity();
      const n = this.restPose[t];
      n?.position && Ar.fromArray(n.position).negate(), n?.rotation && Od(Rr.fromArray(n.rotation)), Ar.add(i.position), Rr.premultiply(i.quaternion), r[t] = { position: Ar.toArray(), rotation: Rr.toArray() };
    }), r;
  }
  setPose(r) {
    Object.entries(r).forEach(([e, t]) => {
      const i = e, n = this.getBoneNode(i);
      if (!n) return;
      const s = this.restPose[i];
      s && (t?.position && (n.position.fromArray(t.position), s.position && n.position.add(Ar.fromArray(s.position))), t?.rotation && (n.quaternion.fromArray(t.rotation), s.rotation && n.quaternion.multiply(Rr.fromArray(s.rotation))));
    });
  }
  resetPose() {
    Object.entries(this.restPose).forEach(([r, e]) => {
      const t = this.getBoneNode(r);
      t && (e?.position && t.position.fromArray(e.position), e?.rotation && t.quaternion.fromArray(e.rotation));
    });
  }
  getBone(r) {
    var e;
    return (e = this.humanBones[r]) != null ? e : void 0;
  }
  getBoneNode(r) {
    var e, t;
    return (t = (e = this.humanBones[r]) == null ? void 0 : e.node) != null ? t : null;
  }
}, bo = new P(), jM = new Ce(), qM = new P(), Kh = class Fd extends Pl {
  static _setupTransforms(e) {
    const t = new at();
    t.name = "VRMHumanoidRig";
    const i = {}, n = {}, s = {};
    Eo.forEach((o) => {
      var l;
      const c = e.getBoneNode(o);
      if (c) {
        const h = new P(), d = new Ce();
        c.updateWorldMatrix(!0, !1), c.matrixWorld.decompose(h, d, bo), i[o] = h, n[o] = c.quaternion.clone();
        const u = new Ce();
        (l = c.parent) == null || l.matrixWorld.decompose(bo, u, bo), s[o] = u;
      }
    });
    const a = {};
    return Eo.forEach((o) => {
      var l;
      const c = e.getBoneNode(o);
      if (c) {
        const h = i[o];
        let d = o, u;
        for (; u == null && (d = XM[d], d != null); ) u = i[d];
        const p = new at();
        p.name = "Normalized_" + c.name, (d ? (l = a[d]) == null ? void 0 : l.node : t).add(p), p.position.copy(h), u && p.position.sub(u), a[o] = { node: p };
      }
    }), { rigBones: a, root: t, parentWorldRotations: s, boneRotations: n };
  }
  constructor(e) {
    const { rigBones: t, root: i, parentWorldRotations: n, boneRotations: s } = Fd._setupTransforms(e);
    super(t), this.original = e, this.root = i, this._parentWorldRotations = n, this._boneRotations = s;
  }
  update() {
    Eo.forEach((e) => {
      const t = this.original.getBoneNode(e);
      if (t != null) {
        const i = this.getBoneNode(e), n = this._parentWorldRotations[e], s = jM.copy(n).invert(), a = this._boneRotations[e];
        if (t.quaternion.copy(i.quaternion).multiply(n).premultiply(s).multiply(a), e === "hips") {
          const o = i.getWorldPosition(qM);
          t.parent.updateWorldMatrix(!0, !1);
          const l = t.parent.matrixWorld, c = o.applyMatrix4(l.invert());
          t.position.copy(c);
        }
      }
    });
  }
}, Zh = class Bd {
  get restPose() {
    return console.warn("VRMHumanoid: restPose is deprecated. Use either rawRestPose or normalizedRestPose instead."), this.rawRestPose;
  }
  get rawRestPose() {
    return this._rawHumanBones.restPose;
  }
  get normalizedRestPose() {
    return this._normalizedHumanBones.restPose;
  }
  get humanBones() {
    return this._rawHumanBones.humanBones;
  }
  get rawHumanBones() {
    return this._rawHumanBones.humanBones;
  }
  get normalizedHumanBones() {
    return this._normalizedHumanBones.humanBones;
  }
  get normalizedHumanBonesRoot() {
    return this._normalizedHumanBones.root;
  }
  constructor(e, t) {
    var i;
    this.autoUpdateHumanBones = (i = t?.autoUpdateHumanBones) != null ? i : !0, this._rawHumanBones = new Pl(e), this._normalizedHumanBones = new Kh(this._rawHumanBones);
  }
  copy(e) {
    return this.autoUpdateHumanBones = e.autoUpdateHumanBones, this._rawHumanBones = new Pl(e.humanBones), this._normalizedHumanBones = new Kh(this._rawHumanBones), this;
  }
  clone() {
    return new Bd(this.humanBones, { autoUpdateHumanBones: this.autoUpdateHumanBones }).copy(this);
  }
  getAbsolutePose() {
    return console.warn("VRMHumanoid: getAbsolutePose() is deprecated. Use either getRawAbsolutePose() or getNormalizedAbsolutePose() instead."), this.getRawAbsolutePose();
  }
  getRawAbsolutePose() {
    return this._rawHumanBones.getAbsolutePose();
  }
  getNormalizedAbsolutePose() {
    return this._normalizedHumanBones.getAbsolutePose();
  }
  getPose() {
    return console.warn("VRMHumanoid: getPose() is deprecated. Use either getRawPose() or getNormalizedPose() instead."), this.getRawPose();
  }
  getRawPose() {
    return this._rawHumanBones.getPose();
  }
  getNormalizedPose() {
    return this._normalizedHumanBones.getPose();
  }
  setPose(e) {
    return console.warn("VRMHumanoid: setPose() is deprecated. Use either setRawPose() or setNormalizedPose() instead."), this.setRawPose(e);
  }
  setRawPose(e) {
    return this._rawHumanBones.setPose(e);
  }
  setNormalizedPose(e) {
    return this._normalizedHumanBones.setPose(e);
  }
  resetPose() {
    return console.warn("VRMHumanoid: resetPose() is deprecated. Use either resetRawPose() or resetNormalizedPose() instead."), this.resetRawPose();
  }
  resetRawPose() {
    return this._rawHumanBones.resetPose();
  }
  resetNormalizedPose() {
    return this._normalizedHumanBones.resetPose();
  }
  getBone(e) {
    return console.warn("VRMHumanoid: getBone() is deprecated. Use either getRawBone() or getNormalizedBone() instead."), this.getRawBone(e);
  }
  getRawBone(e) {
    return this._rawHumanBones.getBone(e);
  }
  getNormalizedBone(e) {
    return this._normalizedHumanBones.getBone(e);
  }
  getBoneNode(e) {
    return console.warn("VRMHumanoid: getBoneNode() is deprecated. Use either getRawBoneNode() or getNormalizedBoneNode() instead."), this.getRawBoneNode(e);
  }
  getRawBoneNode(e) {
    return this._rawHumanBones.getBoneNode(e);
  }
  getNormalizedBoneNode(e) {
    return this._normalizedHumanBones.getBoneNode(e);
  }
  update() {
    this.autoUpdateHumanBones && this._normalizedHumanBones.update();
  }
}, YM = { Hips: "hips", Spine: "spine", Head: "head", LeftUpperLeg: "leftUpperLeg", LeftLowerLeg: "leftLowerLeg", LeftFoot: "leftFoot", RightUpperLeg: "rightUpperLeg", RightLowerLeg: "rightLowerLeg", RightFoot: "rightFoot", LeftUpperArm: "leftUpperArm", LeftLowerArm: "leftLowerArm", LeftHand: "leftHand", RightUpperArm: "rightUpperArm", RightLowerArm: "rightLowerArm", RightHand: "rightHand" }, KM = /* @__PURE__ */ new Set(["1.0", "1.0-beta"]), $h = { leftThumbProximal: "leftThumbMetacarpal", leftThumbIntermediate: "leftThumbProximal", rightThumbProximal: "rightThumbMetacarpal", rightThumbIntermediate: "rightThumbProximal" }, ZM = class {
  get name() {
    return "VRMHumanoidLoaderPlugin";
  }
  constructor(r, e) {
    this.parser = r, this.helperRoot = e?.helperRoot, this.autoUpdateHumanBones = e?.autoUpdateHumanBones;
  }
  afterRoot(r) {
    return rt(this, null, function* () {
      r.userData.vrmHumanoid = yield this._import(r);
    });
  }
  _import(r) {
    return rt(this, null, function* () {
      const e = yield this._v1Import(r);
      return e || (yield this._v0Import(r)) || null;
    });
  }
  _v1Import(r) {
    return rt(this, null, function* () {
      var e, t;
      const i = this.parser.json;
      if (((e = i.extensionsUsed) == null ? void 0 : e.indexOf("VRMC_vrm")) === -1) return null;
      const n = (t = i.extensions) == null ? void 0 : t.VRMC_vrm;
      if (!n) return null;
      const s = n.specVersion;
      if (!KM.has(s)) return console.warn(`VRMHumanoidLoaderPlugin: Unknown VRMC_vrm specVersion "${s}"`), null;
      const a = n.humanoid;
      if (!a) return null;
      const o = a.humanBones.leftThumbIntermediate != null || a.humanBones.rightThumbIntermediate != null, l = {};
      a.humanBones != null && (yield Promise.all(Object.entries(a.humanBones).map((h) => rt(this, [h], function* ([d, u]) {
        let p = d;
        const f = u.node;
        if (o) {
          const m = $h[p];
          m != null && (p = m);
        }
        const v = yield this.parser.getDependency("node", f);
        if (v == null) {
          console.warn(`A glTF node bound to the humanoid bone ${p} (index = ${f}) does not exist`);
          return;
        }
        l[p] = { node: v };
      }))));
      const c = new Zh(this._ensureRequiredBonesExist(l), { autoUpdateHumanBones: this.autoUpdateHumanBones });
      if (r.scene.add(c.normalizedHumanBonesRoot), this.helperRoot) {
        const h = new Yh(c);
        this.helperRoot.add(h), h.renderOrder = this.helperRoot.renderOrder;
      }
      return c;
    });
  }
  _v0Import(r) {
    return rt(this, null, function* () {
      var e;
      const t = (e = this.parser.json.extensions) == null ? void 0 : e.VRM;
      if (!t) return null;
      const i = t.humanoid;
      if (!i) return null;
      const n = {};
      i.humanBones != null && (yield Promise.all(i.humanBones.map((a) => rt(this, null, function* () {
        const o = a.bone, l = a.node;
        if (o == null || l == null) return;
        if (l < 0) {
          console.warn(`A glTF node index for the humanoid bone ${o} is negative (${l}), ignoring this bone.`);
          return;
        }
        const c = yield this.parser.getDependency("node", l);
        if (c == null) {
          console.warn(`A glTF node bound to the humanoid bone ${o} (index = ${l}) does not exist`);
          return;
        }
        const h = $h[o], d = h ?? o;
        if (n[d] != null) {
          console.warn(`Multiple bone entries for ${d} detected (index = ${l}), ignoring duplicated entries.`);
          return;
        }
        n[d] = { node: c };
      }))));
      const s = new Zh(this._ensureRequiredBonesExist(n), { autoUpdateHumanBones: this.autoUpdateHumanBones });
      if (r.scene.add(s.normalizedHumanBonesRoot), this.helperRoot) {
        const a = new Yh(s);
        this.helperRoot.add(a), a.renderOrder = this.helperRoot.renderOrder;
      }
      return s;
    });
  }
  _ensureRequiredBonesExist(r) {
    const e = Object.values(YM).filter((t) => r[t] == null);
    if (e.length > 0) throw new Error(`VRMHumanoidLoaderPlugin: These humanoid bones are required but not exist: ${e.join(", ")}`);
    return r;
  }
}, Qh = class extends yt {
  constructor() {
    super(), this._currentTheta = 0, this._currentRadius = 0, this.theta = 0, this.radius = 0, this._currentTheta = 0, this._currentRadius = 0, this._attrPos = new nt(new Float32Array(65 * 3), 3), this.setAttribute("position", this._attrPos), this._attrIndex = new nt(new Uint16Array(3 * 63), 1), this.setIndex(this._attrIndex), this._buildIndex(), this.update();
  }
  update() {
    let r = !1;
    this._currentTheta !== this.theta && (this._currentTheta = this.theta, r = !0), this._currentRadius !== this.radius && (this._currentRadius = this.radius, r = !0), r && this._buildPosition();
  }
  _buildPosition() {
    this._attrPos.setXYZ(0, 0, 0, 0);
    for (let r = 0; r < 64; r++) {
      const e = r / 63 * this._currentTheta;
      this._attrPos.setXYZ(r + 1, this._currentRadius * Math.sin(e), 0, this._currentRadius * Math.cos(e));
    }
    this._attrPos.needsUpdate = !0;
  }
  _buildIndex() {
    for (let r = 0; r < 63; r++) this._attrIndex.setXYZ(r * 3, 0, r + 1, r + 2);
    this._attrIndex.needsUpdate = !0;
  }
}, $M = class extends yt {
  constructor() {
    super(), this.radius = 0, this._currentRadius = 0, this.tail = new P(), this._currentTail = new P(), this._attrPos = new nt(new Float32Array(294), 3), this.setAttribute("position", this._attrPos), this._attrIndex = new nt(new Uint16Array(194), 1), this.setIndex(this._attrIndex), this._buildIndex(), this.update();
  }
  update() {
    let r = !1;
    this._currentRadius !== this.radius && (this._currentRadius = this.radius, r = !0), this._currentTail.equals(this.tail) || (this._currentTail.copy(this.tail), r = !0), r && this._buildPosition();
  }
  _buildPosition() {
    for (let r = 0; r < 32; r++) {
      const e = r / 16 * Math.PI;
      this._attrPos.setXYZ(r, Math.cos(e), Math.sin(e), 0), this._attrPos.setXYZ(32 + r, 0, Math.cos(e), Math.sin(e)), this._attrPos.setXYZ(64 + r, Math.sin(e), 0, Math.cos(e));
    }
    this.scale(this._currentRadius, this._currentRadius, this._currentRadius), this.translate(this._currentTail.x, this._currentTail.y, this._currentTail.z), this._attrPos.setXYZ(96, 0, 0, 0), this._attrPos.setXYZ(97, this._currentTail.x, this._currentTail.y, this._currentTail.z), this._attrPos.needsUpdate = !0;
  }
  _buildIndex() {
    for (let r = 0; r < 32; r++) {
      const e = (r + 1) % 32;
      this._attrIndex.setXY(r * 2, r, e), this._attrIndex.setXY(64 + r * 2, 32 + r, 32 + e), this._attrIndex.setXY(128 + r * 2, 64 + r, 64 + e);
    }
    this._attrIndex.setXY(192, 96, 97), this._attrIndex.needsUpdate = !0;
  }
}, Ws = new Ce(), Jh = new Ce(), Gn = new P(), eu = new P(), tu = Math.sqrt(2) / 2, QM = new Ce(0, 0, -tu, tu), JM = new P(0, 1, 0), ey = class extends Qt {
  constructor(r) {
    super(), this.matrixAutoUpdate = !1, this.vrmLookAt = r;
    {
      const e = new Qh();
      e.radius = 0.5;
      const t = new Ki({ color: 65280, transparent: !0, opacity: 0.5, side: ai, depthTest: !1, depthWrite: !1 });
      this._meshPitch = new Wt(e, t), this.add(this._meshPitch);
    }
    {
      const e = new Qh();
      e.radius = 0.5;
      const t = new Ki({ color: 16711680, transparent: !0, opacity: 0.5, side: ai, depthTest: !1, depthWrite: !1 });
      this._meshYaw = new Wt(e, t), this.add(this._meshYaw);
    }
    {
      const e = new $M();
      e.radius = 0.1;
      const t = new Dr({ color: 16777215, depthTest: !1, depthWrite: !1 });
      this._lineTarget = new ds(e, t), this._lineTarget.frustumCulled = !1, this.add(this._lineTarget);
    }
  }
  dispose() {
    this._meshYaw.geometry.dispose(), this._meshYaw.material.dispose(), this._meshPitch.geometry.dispose(), this._meshPitch.material.dispose(), this._lineTarget.geometry.dispose(), this._lineTarget.material.dispose();
  }
  updateMatrixWorld(r) {
    const e = mt.DEG2RAD * this.vrmLookAt.yaw;
    this._meshYaw.geometry.theta = e, this._meshYaw.geometry.update();
    const t = mt.DEG2RAD * this.vrmLookAt.pitch;
    this._meshPitch.geometry.theta = t, this._meshPitch.geometry.update(), this.vrmLookAt.getLookAtWorldPosition(Gn), this.vrmLookAt.getLookAtWorldQuaternion(Ws), Ws.multiply(this.vrmLookAt.getFaceFrontQuaternion(Jh)), this._meshYaw.position.copy(Gn), this._meshYaw.quaternion.copy(Ws), this._meshPitch.position.copy(Gn), this._meshPitch.quaternion.copy(Ws), this._meshPitch.quaternion.multiply(Jh.setFromAxisAngle(JM, e)), this._meshPitch.quaternion.multiply(QM);
    const { target: i, autoUpdate: n } = this.vrmLookAt;
    i != null && n && (i.getWorldPosition(eu).sub(Gn), this._lineTarget.geometry.tail.copy(eu), this._lineTarget.geometry.update(), this._lineTarget.position.copy(Gn)), super.updateMatrixWorld(r);
  }
}, ty = new P(), iy = new P();
function Cl(r, e) {
  return r.matrixWorld.decompose(ty, e, iy), e;
}
function ta(r) {
  return [Math.atan2(-r.z, r.x), Math.atan2(r.y, Math.sqrt(r.x * r.x + r.z * r.z))];
}
function iu(r) {
  const e = Math.round(r / 2 / Math.PI);
  return r - 2 * Math.PI * e;
}
var ru = new P(0, 0, 1), ry = new P(), ny = new P(), sy = new P(), ay = new Ce(), wo = new Ce(), nu = new Ce(), oy = new Ce(), Ao = new Yt(), Vd = class Hd {
  constructor(e, t) {
    this.offsetFromHeadBone = new P(), this.autoUpdate = !0, this.faceFront = new P(0, 0, 1), this.humanoid = e, this.applier = t, this._yaw = 0, this._pitch = 0, this._needsUpdate = !0, this._restHeadWorldQuaternion = this.getLookAtWorldQuaternion(new Ce());
  }
  get yaw() {
    return this._yaw;
  }
  set yaw(e) {
    this._yaw = e, this._needsUpdate = !0;
  }
  get pitch() {
    return this._pitch;
  }
  set pitch(e) {
    this._pitch = e, this._needsUpdate = !0;
  }
  get euler() {
    return console.warn("VRMLookAt: euler is deprecated. use getEuler() instead."), this.getEuler(new Yt());
  }
  getEuler(e) {
    return e.set(mt.DEG2RAD * this._pitch, mt.DEG2RAD * this._yaw, 0, "YXZ");
  }
  copy(e) {
    if (this.humanoid !== e.humanoid) throw new Error("VRMLookAt: humanoid must be same in order to copy");
    return this.offsetFromHeadBone.copy(e.offsetFromHeadBone), this.applier = e.applier, this.autoUpdate = e.autoUpdate, this.target = e.target, this.faceFront.copy(e.faceFront), this;
  }
  clone() {
    return new Hd(this.humanoid, this.applier).copy(this);
  }
  reset() {
    this._yaw = 0, this._pitch = 0, this._needsUpdate = !0;
  }
  getLookAtWorldPosition(e) {
    const t = this.humanoid.getRawBoneNode("head");
    return e.copy(this.offsetFromHeadBone).applyMatrix4(t.matrixWorld);
  }
  getLookAtWorldQuaternion(e) {
    const t = this.humanoid.getRawBoneNode("head");
    return Cl(t, e);
  }
  getFaceFrontQuaternion(e) {
    if (this.faceFront.distanceToSquared(ru) < 0.01) return e.copy(this._restHeadWorldQuaternion).invert();
    const [t, i] = ta(this.faceFront);
    return Ao.set(0, 0.5 * Math.PI + t, i, "YZX"), e.setFromEuler(Ao).premultiply(oy.copy(this._restHeadWorldQuaternion).invert());
  }
  getLookAtWorldDirection(e) {
    return this.getLookAtWorldQuaternion(wo), this.getFaceFrontQuaternion(nu), e.copy(ru).applyQuaternion(wo).applyQuaternion(nu).applyEuler(this.getEuler(Ao));
  }
  lookAt(e) {
    const t = ay.copy(this._restHeadWorldQuaternion).multiply(Od(this.getLookAtWorldQuaternion(wo))), i = this.getLookAtWorldPosition(ny), n = sy.copy(e).sub(i).applyQuaternion(t).normalize(), [s, a] = ta(this.faceFront), [o, l] = ta(n), c = iu(o - s), h = iu(a - l);
    this._yaw = mt.RAD2DEG * c, this._pitch = mt.RAD2DEG * h, this._needsUpdate = !0;
  }
  update(e) {
    this.target != null && this.autoUpdate && this.lookAt(this.target.getWorldPosition(ry)), this._needsUpdate && (this._needsUpdate = !1, this.applier.applyYawPitch(this._yaw, this._pitch));
  }
};
Vd.EULER_ORDER = "YXZ";
var ly = Vd, cy = new P(0, 0, 1), bi = new Ce(), Jr = new Ce(), ni = new Yt(0, 0, 0, "YXZ"), ia = class {
  constructor(r, e, t, i, n) {
    this.humanoid = r, this.rangeMapHorizontalInner = e, this.rangeMapHorizontalOuter = t, this.rangeMapVerticalDown = i, this.rangeMapVerticalUp = n, this.faceFront = new P(0, 0, 1), this._restQuatLeftEye = new Ce(), this._restQuatRightEye = new Ce(), this._restLeftEyeParentWorldQuat = new Ce(), this._restRightEyeParentWorldQuat = new Ce();
    const s = this.humanoid.getRawBoneNode("leftEye"), a = this.humanoid.getRawBoneNode("rightEye");
    s && (this._restQuatLeftEye.copy(s.quaternion), Cl(s.parent, this._restLeftEyeParentWorldQuat)), a && (this._restQuatRightEye.copy(a.quaternion), Cl(a.parent, this._restRightEyeParentWorldQuat));
  }
  applyYawPitch(r, e) {
    const t = this.humanoid.getRawBoneNode("leftEye"), i = this.humanoid.getRawBoneNode("rightEye"), n = this.humanoid.getNormalizedBoneNode("leftEye"), s = this.humanoid.getNormalizedBoneNode("rightEye");
    t && (e < 0 ? ni.x = -mt.DEG2RAD * this.rangeMapVerticalDown.map(-e) : ni.x = mt.DEG2RAD * this.rangeMapVerticalUp.map(e), r < 0 ? ni.y = -mt.DEG2RAD * this.rangeMapHorizontalInner.map(-r) : ni.y = mt.DEG2RAD * this.rangeMapHorizontalOuter.map(r), bi.setFromEuler(ni), this._getWorldFaceFrontQuat(Jr), n.quaternion.copy(Jr).multiply(bi).multiply(Jr.invert()), bi.copy(this._restLeftEyeParentWorldQuat), t.quaternion.copy(n.quaternion).multiply(bi).premultiply(bi.invert()).multiply(this._restQuatLeftEye)), i && (e < 0 ? ni.x = -mt.DEG2RAD * this.rangeMapVerticalDown.map(-e) : ni.x = mt.DEG2RAD * this.rangeMapVerticalUp.map(e), r < 0 ? ni.y = -mt.DEG2RAD * this.rangeMapHorizontalOuter.map(-r) : ni.y = mt.DEG2RAD * this.rangeMapHorizontalInner.map(r), bi.setFromEuler(ni), this._getWorldFaceFrontQuat(Jr), s.quaternion.copy(Jr).multiply(bi).multiply(Jr.invert()), bi.copy(this._restRightEyeParentWorldQuat), i.quaternion.copy(s.quaternion).multiply(bi).premultiply(bi.invert()).multiply(this._restQuatRightEye));
  }
  lookAt(r) {
    console.warn("VRMLookAtBoneApplier: lookAt() is deprecated. use apply() instead.");
    const e = mt.RAD2DEG * r.y, t = mt.RAD2DEG * r.x;
    this.applyYawPitch(e, t);
  }
  _getWorldFaceFrontQuat(r) {
    if (this.faceFront.distanceToSquared(cy) < 0.01) return r.identity();
    const [e, t] = ta(this.faceFront);
    return ni.set(0, 0.5 * Math.PI + e, t, "YZX"), r.setFromEuler(ni);
  }
};
ia.type = "bone";
var Ll = class {
  constructor(r, e, t, i, n) {
    this.expressions = r, this.rangeMapHorizontalInner = e, this.rangeMapHorizontalOuter = t, this.rangeMapVerticalDown = i, this.rangeMapVerticalUp = n;
  }
  applyYawPitch(r, e) {
    e < 0 ? (this.expressions.setValue("lookDown", 0), this.expressions.setValue("lookUp", this.rangeMapVerticalUp.map(-e))) : (this.expressions.setValue("lookUp", 0), this.expressions.setValue("lookDown", this.rangeMapVerticalDown.map(e))), r < 0 ? (this.expressions.setValue("lookLeft", 0), this.expressions.setValue("lookRight", this.rangeMapHorizontalOuter.map(-r))) : (this.expressions.setValue("lookRight", 0), this.expressions.setValue("lookLeft", this.rangeMapHorizontalOuter.map(r)));
  }
  lookAt(r) {
    console.warn("VRMLookAtBoneApplier: lookAt() is deprecated. use apply() instead.");
    const e = mt.RAD2DEG * r.y, t = mt.RAD2DEG * r.x;
    this.applyYawPitch(e, t);
  }
};
Ll.type = "expression";
var su = class {
  constructor(r, e) {
    this.inputMaxValue = r, this.outputScale = e;
  }
  map(r) {
    return this.outputScale * Rd(r / this.inputMaxValue);
  }
}, hy = /* @__PURE__ */ new Set(["1.0", "1.0-beta"]), Xs = 0.01, uy = class {
  get name() {
    return "VRMLookAtLoaderPlugin";
  }
  constructor(r, e) {
    this.parser = r, this.helperRoot = e?.helperRoot;
  }
  afterRoot(r) {
    return rt(this, null, function* () {
      const e = r.userData.vrmHumanoid;
      if (e === null) return;
      if (e === void 0) throw new Error("VRMLookAtLoaderPlugin: vrmHumanoid is undefined. VRMHumanoidLoaderPlugin have to be used first");
      const t = r.userData.vrmExpressionManager;
      if (t !== null) {
        if (t === void 0) throw new Error("VRMLookAtLoaderPlugin: vrmExpressionManager is undefined. VRMExpressionLoaderPlugin have to be used first");
        r.userData.vrmLookAt = yield this._import(r, e, t);
      }
    });
  }
  _import(r, e, t) {
    return rt(this, null, function* () {
      if (e == null || t == null) return null;
      const i = yield this._v1Import(r, e, t);
      return i || (yield this._v0Import(r, e, t)) || null;
    });
  }
  _v1Import(r, e, t) {
    return rt(this, null, function* () {
      var i, n, s;
      const a = this.parser.json;
      if (((i = a.extensionsUsed) == null ? void 0 : i.indexOf("VRMC_vrm")) === -1) return null;
      const o = (n = a.extensions) == null ? void 0 : n.VRMC_vrm;
      if (!o) return null;
      const l = o.specVersion;
      if (!hy.has(l)) return console.warn(`VRMLookAtLoaderPlugin: Unknown VRMC_vrm specVersion "${l}"`), null;
      const c = o.lookAt;
      if (!c) return null;
      const h = c.type === "expression" ? 1 : 10, d = this._v1ImportRangeMap(c.rangeMapHorizontalInner, h), u = this._v1ImportRangeMap(c.rangeMapHorizontalOuter, h), p = this._v1ImportRangeMap(c.rangeMapVerticalDown, h), f = this._v1ImportRangeMap(c.rangeMapVerticalUp, h);
      let v;
      c.type === "expression" ? v = new Ll(t, d, u, p, f) : v = new ia(e, d, u, p, f);
      const m = this._importLookAt(e, v);
      return m.offsetFromHeadBone.fromArray((s = c.offsetFromHeadBone) != null ? s : [0, 0.06, 0]), m;
    });
  }
  _v1ImportRangeMap(r, e) {
    var t, i;
    let n = (t = r?.inputMaxValue) != null ? t : 90;
    const s = (i = r?.outputScale) != null ? i : e;
    return n < Xs && (console.warn("VRMLookAtLoaderPlugin: inputMaxValue of a range map is too small. Consider reviewing the range map!"), n = Xs), new su(n, s);
  }
  _v0Import(r, e, t) {
    return rt(this, null, function* () {
      var i, n, s, a;
      const o = (i = this.parser.json.extensions) == null ? void 0 : i.VRM;
      if (!o) return null;
      const l = o.firstPerson;
      if (!l) return null;
      const c = l.lookAtTypeName === "BlendShape" ? 1 : 10, h = this._v0ImportDegreeMap(l.lookAtHorizontalInner, c), d = this._v0ImportDegreeMap(l.lookAtHorizontalOuter, c), u = this._v0ImportDegreeMap(l.lookAtVerticalDown, c), p = this._v0ImportDegreeMap(l.lookAtVerticalUp, c);
      let f;
      l.lookAtTypeName === "BlendShape" ? f = new Ll(t, h, d, u, p) : f = new ia(e, h, d, u, p);
      const v = this._importLookAt(e, f);
      return l.firstPersonBoneOffset ? v.offsetFromHeadBone.set((n = l.firstPersonBoneOffset.x) != null ? n : 0, (s = l.firstPersonBoneOffset.y) != null ? s : 0.06, -((a = l.firstPersonBoneOffset.z) != null ? a : 0)) : v.offsetFromHeadBone.set(0, 0.06, 0), v.faceFront.set(0, 0, -1), f instanceof ia && f.faceFront.set(0, 0, -1), v;
    });
  }
  _v0ImportDegreeMap(r, e) {
    var t, i;
    const n = r?.curve;
    JSON.stringify(n) !== "[0,0,0,1,1,1,1,0]" && console.warn("Curves of LookAtDegreeMap defined in VRM 0.0 are not supported");
    let s = (t = r?.xRange) != null ? t : 90;
    const a = (i = r?.yRange) != null ? i : e;
    return s < Xs && (console.warn("VRMLookAtLoaderPlugin: xRange of a degree map is too small. Consider reviewing the degree map!"), s = Xs), new su(s, a);
  }
  _importLookAt(r, e) {
    const t = new ly(r, e);
    if (this.helperRoot) {
      const i = new ey(t);
      this.helperRoot.add(i), i.renderOrder = this.helperRoot.renderOrder;
    }
    return t;
  }
};
function dy(r, e) {
  return typeof r != "string" || r === "" ? "" : (/^https?:\/\//i.test(e) && /^\//.test(r) && (e = e.replace(/(^https?:\/\/[^/]+).*/i, "$1")), /^(https?:)?\/\//i.test(r) || /^data:.*,.*$/i.test(r) || /^blob:.*$/i.test(r) ? r : e + r);
}
var py = /* @__PURE__ */ new Set(["1.0", "1.0-beta"]), fy = class {
  get name() {
    return "VRMMetaLoaderPlugin";
  }
  constructor(r, e) {
    var t, i, n;
    this.parser = r, this.needThumbnailImage = (t = e?.needThumbnailImage) != null ? t : !1, this.acceptLicenseUrls = (i = e?.acceptLicenseUrls) != null ? i : ["https://vrm.dev/licenses/1.0/"], this.acceptV0Meta = (n = e?.acceptV0Meta) != null ? n : !0;
  }
  afterRoot(r) {
    return rt(this, null, function* () {
      r.userData.vrmMeta = yield this._import(r);
    });
  }
  _import(r) {
    return rt(this, null, function* () {
      const e = yield this._v1Import(r);
      return e ?? (yield this._v0Import(r)) ?? null;
    });
  }
  _v1Import(r) {
    return rt(this, null, function* () {
      var e, t, i;
      const n = this.parser.json;
      if (((e = n.extensionsUsed) == null ? void 0 : e.indexOf("VRMC_vrm")) === -1) return null;
      const s = (t = n.extensions) == null ? void 0 : t.VRMC_vrm;
      if (s == null) return null;
      const a = s.specVersion;
      if (!py.has(a)) return console.warn(`VRMMetaLoaderPlugin: Unknown VRMC_vrm specVersion "${a}"`), null;
      const o = s.meta;
      if (!o) return null;
      const l = o.licenseUrl;
      if (!new Set(this.acceptLicenseUrls).has(l)) throw new Error(`VRMMetaLoaderPlugin: The license url "${l}" is not accepted`);
      let c;
      return this.needThumbnailImage && o.thumbnailImage != null && (c = (i = yield this._extractGLTFImage(o.thumbnailImage)) != null ? i : void 0), { metaVersion: "1", name: o.name, version: o.version, authors: o.authors, copyrightInformation: o.copyrightInformation, contactInformation: o.contactInformation, references: o.references, thirdPartyLicenses: o.thirdPartyLicenses, thumbnailImage: c, licenseUrl: o.licenseUrl, avatarPermission: o.avatarPermission, allowExcessivelyViolentUsage: o.allowExcessivelyViolentUsage, allowExcessivelySexualUsage: o.allowExcessivelySexualUsage, commercialUsage: o.commercialUsage, allowPoliticalOrReligiousUsage: o.allowPoliticalOrReligiousUsage, allowAntisocialOrHateUsage: o.allowAntisocialOrHateUsage, creditNotation: o.creditNotation, allowRedistribution: o.allowRedistribution, modification: o.modification, otherLicenseUrl: o.otherLicenseUrl };
    });
  }
  _v0Import(r) {
    return rt(this, null, function* () {
      var e;
      const t = (e = this.parser.json.extensions) == null ? void 0 : e.VRM;
      if (!t) return null;
      const i = t.meta;
      if (!i) return null;
      if (!this.acceptV0Meta) throw new Error("VRMMetaLoaderPlugin: Attempted to load VRM0.0 meta but acceptV0Meta is false");
      let n;
      return this.needThumbnailImage && i.texture != null && i.texture !== -1 && (n = yield this.parser.getDependency("texture", i.texture)), { metaVersion: "0", allowedUserName: i.allowedUserName, author: i.author, commercialUssageName: i.commercialUssageName, contactInformation: i.contactInformation, licenseName: i.licenseName, otherLicenseUrl: i.otherLicenseUrl, otherPermissionUrl: i.otherPermissionUrl, reference: i.reference, sexualUssageName: i.sexualUssageName, texture: n ?? void 0, title: i.title, version: i.version, violentUssageName: i.violentUssageName };
    });
  }
  _extractGLTFImage(r) {
    return rt(this, null, function* () {
      var e;
      const t = (e = this.parser.json.images) == null ? void 0 : e[r];
      if (t == null) return console.warn(`VRMMetaLoaderPlugin: Attempt to use images[${r}] of glTF as a thumbnail but the image doesn't exist`), null;
      let i = t.uri;
      if (t.bufferView != null) {
        const n = yield this.parser.getDependency("bufferView", t.bufferView), s = new Blob([n], { type: t.mimeType });
        i = URL.createObjectURL(s);
      }
      return i == null ? (console.warn(`VRMMetaLoaderPlugin: Attempt to use images[${r}] of glTF as a thumbnail but the image couldn't load properly`), null) : yield new ud().loadAsync(dy(i, this.parser.options.path)).catch((n) => (console.error(n), console.warn("VRMMetaLoaderPlugin: Failed to load a thumbnail image"), null));
    });
  }
}, my = class {
  constructor(r) {
    this.scene = r.scene, this.meta = r.meta, this.humanoid = r.humanoid, this.expressionManager = r.expressionManager, this.firstPerson = r.firstPerson, this.lookAt = r.lookAt;
  }
  update(r) {
    this.humanoid.update(), this.lookAt && this.lookAt.update(r), this.expressionManager && this.expressionManager.update();
  }
}, gy = class extends my {
  constructor(r) {
    super(r), this.materials = r.materials, this.springBoneManager = r.springBoneManager, this.nodeConstraintManager = r.nodeConstraintManager;
  }
  update(r) {
    super.update(r), this.nodeConstraintManager && this.nodeConstraintManager.update(), this.springBoneManager && this.springBoneManager.update(r), this.materials && this.materials.forEach((e) => {
      e.update && e.update(r);
    });
  }
}, _y = Object.defineProperty, au = Object.getOwnPropertySymbols, vy = Object.prototype.hasOwnProperty, xy = Object.prototype.propertyIsEnumerable, ou = (r, e, t) => e in r ? _y(r, e, { enumerable: !0, configurable: !0, writable: !0, value: t }) : r[e] = t, lu = (r, e) => {
  for (var t in e || (e = {})) vy.call(e, t) && ou(r, t, e[t]);
  if (au) for (var t of au(e)) xy.call(e, t) && ou(r, t, e[t]);
  return r;
}, Lr = (r, e, t) => new Promise((i, n) => {
  var s = (l) => {
    try {
      o(t.next(l));
    } catch (c) {
      n(c);
    }
  }, a = (l) => {
    try {
      o(t.throw(l));
    } catch (c) {
      n(c);
    }
  }, o = (l) => l.done ? i(l.value) : Promise.resolve(l.value).then(s, a);
  o((t = t.apply(r, e)).next());
}), My = { "": 3e3, srgb: 3001 };
function yy(r, e) {
  parseInt(Ir, 10) >= 152 ? r.colorSpace = e : r.encoding = My[e];
}
var Sy = class {
  get pending() {
    return Promise.all(this._pendings);
  }
  constructor(r, e) {
    this._parser = r, this._materialParams = e, this._pendings = [];
  }
  assignPrimitive(r, e) {
    e != null && (this._materialParams[r] = e);
  }
  assignColor(r, e, t) {
    if (e != null) {
      const i = new be().fromArray(e);
      t && i.convertSRGBToLinear(), this._materialParams[r] = i;
    }
  }
  assignTexture(r, e, t) {
    return Lr(this, null, function* () {
      const i = Lr(this, null, function* () {
        if (e != null) {
          const n = yield this._parser.assignTexture(this._materialParams, r, e);
          if (n == null) {
            console.warn("GLTFMToonMaterialParamsAssignHelper: Failed to load texture. The rendering result may be wrong");
            return;
          }
          t && yy(n, "srgb");
        }
      });
      return this._pendings.push(i), i;
    });
  }
  assignTextureByIndex(r, e, t) {
    return Lr(this, null, function* () {
      return this.assignTexture(r, e != null ? { index: e } : void 0, t);
    });
  }
}, Ty = `// #define PHONG

varying vec3 vViewPosition;

#ifndef FLAT_SHADED
  varying vec3 vNormal;
#endif

#include <common>

// #include <uv_pars_vertex>
#ifdef MTOON_USE_UV
  varying vec2 vUv;

  // COMPAT: pre-r151 uses a common uvTransform
  #if THREE_VRM_THREE_REVISION < 151
    uniform mat3 uvTransform;
  #endif
#endif

// #include <uv2_pars_vertex>
// COMAPT: pre-r151 uses uv2 for lightMap and aoMap
#if THREE_VRM_THREE_REVISION < 151
  #if defined( USE_LIGHTMAP ) || defined( USE_AOMAP )
    attribute vec2 uv2;
    varying vec2 vUv2;
    uniform mat3 uv2Transform;
  #endif
#endif

// #include <displacementmap_pars_vertex>
// #include <envmap_pars_vertex>
#include <color_pars_vertex>
#include <fog_pars_vertex>
#include <morphtarget_pars_vertex>
#include <skinning_pars_vertex>
#include <shadowmap_pars_vertex>
#include <logdepthbuf_pars_vertex>
#include <clipping_planes_pars_vertex>

#ifdef USE_OUTLINEWIDTHMULTIPLYTEXTURE
  uniform sampler2D outlineWidthMultiplyTexture;
  uniform mat3 outlineWidthMultiplyTextureUvTransform;
#endif

uniform float outlineWidthFactor;

void main() {

  // #include <uv_vertex>
  #ifdef MTOON_USE_UV
    // COMPAT: pre-r151 uses a common uvTransform
    #if THREE_VRM_THREE_REVISION >= 151
      vUv = uv;
    #else
      vUv = ( uvTransform * vec3( uv, 1 ) ).xy;
    #endif
  #endif

  // #include <uv2_vertex>
  // COMAPT: pre-r151 uses uv2 for lightMap and aoMap
  #if THREE_VRM_THREE_REVISION < 151
    #if defined( USE_LIGHTMAP ) || defined( USE_AOMAP )
      vUv2 = ( uv2Transform * vec3( uv2, 1 ) ).xy;
    #endif
  #endif

  #include <color_vertex>

  #include <beginnormal_vertex>
  #include <morphnormal_vertex>
  #include <skinbase_vertex>
  #include <skinnormal_vertex>

  // we need this to compute the outline properly
  objectNormal = normalize( objectNormal );

  #include <defaultnormal_vertex>

  #ifndef FLAT_SHADED // Normal computed with derivatives when FLAT_SHADED
    vNormal = normalize( transformedNormal );
  #endif

  #include <begin_vertex>

  #include <morphtarget_vertex>
  #include <skinning_vertex>
  // #include <displacementmap_vertex>
  #include <project_vertex>
  #include <logdepthbuf_vertex>
  #include <clipping_planes_vertex>

  vViewPosition = - mvPosition.xyz;

  #ifdef OUTLINE
    float worldNormalLength = length( transformedNormal );
    vec3 outlineOffset = outlineWidthFactor * worldNormalLength * objectNormal;

    #ifdef USE_OUTLINEWIDTHMULTIPLYTEXTURE
      vec2 outlineWidthMultiplyTextureUv = ( outlineWidthMultiplyTextureUvTransform * vec3( vUv, 1 ) ).xy;
      float outlineTex = texture2D( outlineWidthMultiplyTexture, outlineWidthMultiplyTextureUv ).g;
      outlineOffset *= outlineTex;
    #endif

    #ifdef OUTLINE_WIDTH_SCREEN
      outlineOffset *= vViewPosition.z / projectionMatrix[ 1 ].y;
    #endif

    gl_Position = projectionMatrix * modelViewMatrix * vec4( outlineOffset + transformed, 1.0 );

    gl_Position.z += 1E-6 * gl_Position.w; // anti-artifact magic
  #endif

  #include <worldpos_vertex>
  // #include <envmap_vertex>
  #include <shadowmap_vertex>
  #include <fog_vertex>

}`, Ey = `// #define PHONG

uniform vec3 litFactor;

uniform float opacity;

uniform vec3 shadeColorFactor;
#ifdef USE_SHADEMULTIPLYTEXTURE
  uniform sampler2D shadeMultiplyTexture;
  uniform mat3 shadeMultiplyTextureUvTransform;
#endif

uniform float shadingShiftFactor;
uniform float shadingToonyFactor;

#ifdef USE_SHADINGSHIFTTEXTURE
  uniform sampler2D shadingShiftTexture;
  uniform mat3 shadingShiftTextureUvTransform;
  uniform float shadingShiftTextureScale;
#endif

uniform float giEqualizationFactor;

uniform vec3 parametricRimColorFactor;
#ifdef USE_RIMMULTIPLYTEXTURE
  uniform sampler2D rimMultiplyTexture;
  uniform mat3 rimMultiplyTextureUvTransform;
#endif
uniform float rimLightingMixFactor;
uniform float parametricRimFresnelPowerFactor;
uniform float parametricRimLiftFactor;

#ifdef USE_MATCAPTEXTURE
  uniform vec3 matcapFactor;
  uniform sampler2D matcapTexture;
  uniform mat3 matcapTextureUvTransform;
#endif

uniform vec3 emissive;
uniform float emissiveIntensity;

uniform vec3 outlineColorFactor;
uniform float outlineLightingMixFactor;

#ifdef USE_UVANIMATIONMASKTEXTURE
  uniform sampler2D uvAnimationMaskTexture;
  uniform mat3 uvAnimationMaskTextureUvTransform;
#endif

uniform float uvAnimationScrollXOffset;
uniform float uvAnimationScrollYOffset;
uniform float uvAnimationRotationPhase;

#include <common>
#include <packing>
#include <dithering_pars_fragment>
#include <color_pars_fragment>

// #include <uv_pars_fragment>
#if ( defined( MTOON_USE_UV ) && !defined( MTOON_UVS_VERTEX_ONLY ) )
  varying vec2 vUv;
#endif

// #include <uv2_pars_fragment>
// COMAPT: pre-r151 uses uv2 for lightMap and aoMap
#if THREE_VRM_THREE_REVISION < 151
  #if defined( USE_LIGHTMAP ) || defined( USE_AOMAP )
    varying vec2 vUv2;
  #endif
#endif

#include <map_pars_fragment>

#ifdef USE_MAP
  uniform mat3 mapUvTransform;
#endif

// #include <alphamap_pars_fragment>

#include <alphatest_pars_fragment>

#include <aomap_pars_fragment>
// #include <lightmap_pars_fragment>
#include <emissivemap_pars_fragment>

#ifdef USE_EMISSIVEMAP
  uniform mat3 emissiveMapUvTransform;
#endif

// #include <envmap_common_pars_fragment>
// #include <envmap_pars_fragment>
// #include <cube_uv_reflection_fragment>
#include <fog_pars_fragment>

// #include <bsdfs>
// COMPAT: pre-r151 doesn't have BRDF_Lambert in <common>
#if THREE_VRM_THREE_REVISION < 151
  vec3 BRDF_Lambert( const in vec3 diffuseColor ) {
    return RECIPROCAL_PI * diffuseColor;
  }
#endif

#include <lights_pars_begin>

#include <normal_pars_fragment>

// #include <lights_phong_pars_fragment>
varying vec3 vViewPosition;

struct MToonMaterial {
  vec3 diffuseColor;
  vec3 shadeColor;
  float shadingShift;
};

float linearstep( float a, float b, float t ) {
  return clamp( ( t - a ) / ( b - a ), 0.0, 1.0 );
}

/**
 * Convert NdotL into toon shading factor using shadingShift and shadingToony
 */
float getShading(
  const in float dotNL,
  const in float shadow,
  const in float shadingShift
) {
  float shading = dotNL;
  shading = shading + shadingShift;
  shading = linearstep( -1.0 + shadingToonyFactor, 1.0 - shadingToonyFactor, shading );
  shading *= shadow;
  return shading;
}

/**
 * Mix diffuseColor and shadeColor using shading factor and light color
 */
vec3 getDiffuse(
  const in MToonMaterial material,
  const in float shading,
  in vec3 lightColor
) {
  #ifdef DEBUG_LITSHADERATE
    return vec3( BRDF_Lambert( shading * lightColor ) );
  #endif

  vec3 col = lightColor * BRDF_Lambert( mix( material.shadeColor, material.diffuseColor, shading ) );

  // The "comment out if you want to PBR absolutely" line
  #ifdef V0_COMPAT_SHADE
    col = min( col, material.diffuseColor );
  #endif

  return col;
}

// COMPAT: pre-r156 uses a struct GeometricContext
#if THREE_VRM_THREE_REVISION >= 157
  void RE_Direct_MToon( const in IncidentLight directLight, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in MToonMaterial material, const in float shadow, inout ReflectedLight reflectedLight ) {
    float dotNL = clamp( dot( geometryNormal, directLight.direction ), -1.0, 1.0 );
    vec3 irradiance = directLight.color;

    // directSpecular will be used for rim lighting, not an actual specular
    reflectedLight.directSpecular += irradiance;

    irradiance *= dotNL;

    float shading = getShading( dotNL, shadow, material.shadingShift );

    // toon shaded diffuse
    reflectedLight.directDiffuse += getDiffuse( material, shading, directLight.color );
  }

  void RE_IndirectDiffuse_MToon( const in vec3 irradiance, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in MToonMaterial material, inout ReflectedLight reflectedLight ) {
    // indirect diffuse will use diffuseColor, no shadeColor involved
    reflectedLight.indirectDiffuse += irradiance * BRDF_Lambert( material.diffuseColor );

    // directSpecular will be used for rim lighting, not an actual specular
    reflectedLight.directSpecular += irradiance;
  }
#else
  void RE_Direct_MToon( const in IncidentLight directLight, const in GeometricContext geometry, const in MToonMaterial material, const in float shadow, inout ReflectedLight reflectedLight ) {
    float dotNL = clamp( dot( geometry.normal, directLight.direction ), -1.0, 1.0 );
    vec3 irradiance = directLight.color;

    // directSpecular will be used for rim lighting, not an actual specular
    reflectedLight.directSpecular += irradiance;

    irradiance *= dotNL;

    float shading = getShading( dotNL, shadow, material.shadingShift );

    // toon shaded diffuse
    reflectedLight.directDiffuse += getDiffuse( material, shading, directLight.color );
  }

  void RE_IndirectDiffuse_MToon( const in vec3 irradiance, const in GeometricContext geometry, const in MToonMaterial material, inout ReflectedLight reflectedLight ) {
    // indirect diffuse will use diffuseColor, no shadeColor involved
    reflectedLight.indirectDiffuse += irradiance * BRDF_Lambert( material.diffuseColor );

    // directSpecular will be used for rim lighting, not an actual specular
    reflectedLight.directSpecular += irradiance;
  }
#endif

#define RE_Direct RE_Direct_MToon
#define RE_IndirectDiffuse RE_IndirectDiffuse_MToon
#define Material_LightProbeLOD( material ) (0)

#include <shadowmap_pars_fragment>
// #include <bumpmap_pars_fragment>

// #include <normalmap_pars_fragment>
#ifdef USE_NORMALMAP

  uniform sampler2D normalMap;
  uniform mat3 normalMapUvTransform;
  uniform vec2 normalScale;

#endif

// COMPAT: pre-r151
// USE_NORMALMAP_OBJECTSPACE used to be OBJECTSPACE_NORMALMAP in pre-r151
#if defined( USE_NORMALMAP_OBJECTSPACE ) || defined( OBJECTSPACE_NORMALMAP )

  uniform mat3 normalMatrix;

#endif

// COMPAT: pre-r151
// USE_NORMALMAP_TANGENTSPACE used to be TANGENTSPACE_NORMALMAP in pre-r151
#if ! defined ( USE_TANGENT ) && ( defined ( USE_NORMALMAP_TANGENTSPACE ) || defined ( TANGENTSPACE_NORMALMAP ) )

  // Per-Pixel Tangent Space Normal Mapping
  // http://hacksoflife.blogspot.ch/2009/11/per-pixel-tangent-space-normal-mapping.html

  // three-vrm specific change: it requires \`uv\` as an input in order to support uv scrolls

  // Temporary compat against shader change @ Three.js r126, r151
  #if THREE_VRM_THREE_REVISION >= 151

    mat3 getTangentFrame( vec3 eye_pos, vec3 surf_norm, vec2 uv ) {

      vec3 q0 = dFdx( eye_pos.xyz );
      vec3 q1 = dFdy( eye_pos.xyz );
      vec2 st0 = dFdx( uv.st );
      vec2 st1 = dFdy( uv.st );

      vec3 N = surf_norm;

      vec3 q1perp = cross( q1, N );
      vec3 q0perp = cross( N, q0 );

      vec3 T = q1perp * st0.x + q0perp * st1.x;
      vec3 B = q1perp * st0.y + q0perp * st1.y;

      float det = max( dot( T, T ), dot( B, B ) );
      float scale = ( det == 0.0 ) ? 0.0 : inversesqrt( det );

      return mat3( T * scale, B * scale, N );

    }

  #else

    vec3 perturbNormal2Arb( vec2 uv, vec3 eye_pos, vec3 surf_norm, vec3 mapN, float faceDirection ) {

      vec3 q0 = vec3( dFdx( eye_pos.x ), dFdx( eye_pos.y ), dFdx( eye_pos.z ) );
      vec3 q1 = vec3( dFdy( eye_pos.x ), dFdy( eye_pos.y ), dFdy( eye_pos.z ) );
      vec2 st0 = dFdx( uv.st );
      vec2 st1 = dFdy( uv.st );

      vec3 N = normalize( surf_norm );

      vec3 q1perp = cross( q1, N );
      vec3 q0perp = cross( N, q0 );

      vec3 T = q1perp * st0.x + q0perp * st1.x;
      vec3 B = q1perp * st0.y + q0perp * st1.y;

      // three-vrm specific change: Workaround for the issue that happens when delta of uv = 0.0
      // TODO: Is this still required? Or shall I make a PR about it?
      if ( length( T ) == 0.0 || length( B ) == 0.0 ) {
        return surf_norm;
      }

      float det = max( dot( T, T ), dot( B, B ) );
      float scale = ( det == 0.0 ) ? 0.0 : faceDirection * inversesqrt( det );

      return normalize( T * ( mapN.x * scale ) + B * ( mapN.y * scale ) + N * mapN.z );

    }

  #endif

#endif

// #include <specularmap_pars_fragment>
#include <logdepthbuf_pars_fragment>
#include <clipping_planes_pars_fragment>

// == post correction ==========================================================
void postCorrection() {
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
  #include <fog_fragment>
  #include <premultiplied_alpha_fragment>
  #include <dithering_fragment>
}

// == main procedure ===========================================================
void main() {
  #include <clipping_planes_fragment>

  vec2 uv = vec2(0.5, 0.5);

  #if ( defined( MTOON_USE_UV ) && !defined( MTOON_UVS_VERTEX_ONLY ) )
    uv = vUv;

    float uvAnimMask = 1.0;
    #ifdef USE_UVANIMATIONMASKTEXTURE
      vec2 uvAnimationMaskTextureUv = ( uvAnimationMaskTextureUvTransform * vec3( uv, 1 ) ).xy;
      uvAnimMask = texture2D( uvAnimationMaskTexture, uvAnimationMaskTextureUv ).b;
    #endif

    float uvRotCos = cos( uvAnimationRotationPhase * uvAnimMask );
    float uvRotSin = sin( uvAnimationRotationPhase * uvAnimMask );
    uv = mat2( uvRotCos, -uvRotSin, uvRotSin, uvRotCos ) * ( uv - 0.5 ) + 0.5;
    uv = uv + vec2( uvAnimationScrollXOffset, uvAnimationScrollYOffset ) * uvAnimMask;
  #endif

  #ifdef DEBUG_UV
    gl_FragColor = vec4( 0.0, 0.0, 0.0, 1.0 );
    #if ( defined( MTOON_USE_UV ) && !defined( MTOON_UVS_VERTEX_ONLY ) )
      gl_FragColor = vec4( uv, 0.0, 1.0 );
    #endif
    return;
  #endif

  vec4 diffuseColor = vec4( litFactor, opacity );
  ReflectedLight reflectedLight = ReflectedLight( vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ), vec3( 0.0 ) );
  vec3 totalEmissiveRadiance = emissive * emissiveIntensity;

  #include <logdepthbuf_fragment>

  // #include <map_fragment>
  #ifdef USE_MAP
    vec2 mapUv = ( mapUvTransform * vec3( uv, 1 ) ).xy;
    vec4 sampledDiffuseColor = texture2D( map, mapUv );
    #ifdef DECODE_VIDEO_TEXTURE
      sampledDiffuseColor = vec4( mix( pow( sampledDiffuseColor.rgb * 0.9478672986 + vec3( 0.0521327014 ), vec3( 2.4 ) ), sampledDiffuseColor.rgb * 0.0773993808, vec3( lessThanEqual( sampledDiffuseColor.rgb, vec3( 0.04045 ) ) ) ), sampledDiffuseColor.w );
    #endif
    diffuseColor *= sampledDiffuseColor;
  #endif

  // #include <color_fragment>
  #if ( defined( USE_COLOR ) && !defined( IGNORE_VERTEX_COLOR ) )
    diffuseColor.rgb *= vColor;
  #endif

  // #include <alphamap_fragment>

  #include <alphatest_fragment>

  // #include <specularmap_fragment>

  // #include <normal_fragment_begin>
  float faceDirection = gl_FrontFacing ? 1.0 : -1.0;

  #ifdef FLAT_SHADED

    vec3 fdx = dFdx( vViewPosition );
    vec3 fdy = dFdy( vViewPosition );
    vec3 normal = normalize( cross( fdx, fdy ) );

  #else

    vec3 normal = normalize( vNormal );

    #ifdef DOUBLE_SIDED

      normal *= faceDirection;

    #endif

  #endif

  #ifdef USE_NORMALMAP

    vec2 normalMapUv = ( normalMapUvTransform * vec3( uv, 1 ) ).xy;

  #endif

  #ifdef USE_NORMALMAP_TANGENTSPACE

    #ifdef USE_TANGENT

      mat3 tbn = mat3( normalize( vTangent ), normalize( vBitangent ), normal );

    #else

      mat3 tbn = getTangentFrame( - vViewPosition, normal, normalMapUv );

    #endif

    #if defined( DOUBLE_SIDED ) && ! defined( FLAT_SHADED )

      tbn[0] *= faceDirection;
      tbn[1] *= faceDirection;

    #endif

  #endif

  #ifdef USE_CLEARCOAT_NORMALMAP

    #ifdef USE_TANGENT

      mat3 tbn2 = mat3( normalize( vTangent ), normalize( vBitangent ), normal );

    #else

      mat3 tbn2 = getTangentFrame( - vViewPosition, normal, vClearcoatNormalMapUv );

    #endif

    #if defined( DOUBLE_SIDED ) && ! defined( FLAT_SHADED )

      tbn2[0] *= faceDirection;
      tbn2[1] *= faceDirection;

    #endif

  #endif

  // non perturbed normal for clearcoat among others

  vec3 nonPerturbedNormal = normal;

  #ifdef OUTLINE
    normal *= -1.0;
  #endif

  // #include <normal_fragment_maps>

  // COMPAT: pre-r151
  // USE_NORMALMAP_OBJECTSPACE used to be OBJECTSPACE_NORMALMAP in pre-r151
  #if defined( USE_NORMALMAP_OBJECTSPACE ) || defined( OBJECTSPACE_NORMALMAP )

    normal = texture2D( normalMap, normalMapUv ).xyz * 2.0 - 1.0; // overrides both flatShading and attribute normals

    #ifdef FLIP_SIDED

      normal = - normal;

    #endif

    #ifdef DOUBLE_SIDED

      normal = normal * faceDirection;

    #endif

    normal = normalize( normalMatrix * normal );

  // COMPAT: pre-r151
  // USE_NORMALMAP_TANGENTSPACE used to be TANGENTSPACE_NORMALMAP in pre-r151
  #elif defined( USE_NORMALMAP_TANGENTSPACE ) || defined( TANGENTSPACE_NORMALMAP )

    vec3 mapN = texture2D( normalMap, normalMapUv ).xyz * 2.0 - 1.0;
    mapN.xy *= normalScale;

    // COMPAT: pre-r151
    #if THREE_VRM_THREE_REVISION >= 151 || defined( USE_TANGENT )

      normal = normalize( tbn * mapN );

    #else

      normal = perturbNormal2Arb( uv, -vViewPosition, normal, mapN, faceDirection );

    #endif

  #endif

  // #include <emissivemap_fragment>
  #ifdef USE_EMISSIVEMAP
    vec2 emissiveMapUv = ( emissiveMapUvTransform * vec3( uv, 1 ) ).xy;
    totalEmissiveRadiance *= texture2D( emissiveMap, emissiveMapUv ).rgb;
  #endif

  #ifdef DEBUG_NORMAL
    gl_FragColor = vec4( 0.5 + 0.5 * normal, 1.0 );
    return;
  #endif

  // -- MToon: lighting --------------------------------------------------------
  // accumulation
  // #include <lights_phong_fragment>
  MToonMaterial material;

  material.diffuseColor = diffuseColor.rgb;

  material.shadeColor = shadeColorFactor;
  #ifdef USE_SHADEMULTIPLYTEXTURE
    vec2 shadeMultiplyTextureUv = ( shadeMultiplyTextureUvTransform * vec3( uv, 1 ) ).xy;
    material.shadeColor *= texture2D( shadeMultiplyTexture, shadeMultiplyTextureUv ).rgb;
  #endif

  #if ( defined( USE_COLOR ) && !defined( IGNORE_VERTEX_COLOR ) )
    material.shadeColor.rgb *= vColor;
  #endif

  material.shadingShift = shadingShiftFactor;
  #ifdef USE_SHADINGSHIFTTEXTURE
    vec2 shadingShiftTextureUv = ( shadingShiftTextureUvTransform * vec3( uv, 1 ) ).xy;
    material.shadingShift += texture2D( shadingShiftTexture, shadingShiftTextureUv ).r * shadingShiftTextureScale;
  #endif

  // #include <lights_fragment_begin>

  // MToon Specific changes:
  // Since we want to take shadows into account of shading instead of irradiance,
  // we had to modify the codes that multiplies the results of shadowmap into color of direct lights.

  // COMPAT: pre-r156 uses a struct GeometricContext
  #if THREE_VRM_THREE_REVISION >= 157
    vec3 geometryPosition = - vViewPosition;
    vec3 geometryNormal = normal;
    vec3 geometryViewDir = ( isOrthographic ) ? vec3( 0, 0, 1 ) : normalize( vViewPosition );

    vec3 geometryClearcoatNormal;

    #ifdef USE_CLEARCOAT

      geometryClearcoatNormal = clearcoatNormal;

    #endif
  #else
    GeometricContext geometry;

    geometry.position = - vViewPosition;
    geometry.normal = normal;
    geometry.viewDir = ( isOrthographic ) ? vec3( 0, 0, 1 ) : normalize( vViewPosition );

    #ifdef USE_CLEARCOAT

      geometry.clearcoatNormal = clearcoatNormal;

    #endif
  #endif

  IncidentLight directLight;

  // since these variables will be used in unrolled loop, we have to define in prior
  float shadow;

  #if ( NUM_POINT_LIGHTS > 0 ) && defined( RE_Direct )

    PointLight pointLight;
    #if defined( USE_SHADOWMAP ) && NUM_POINT_LIGHT_SHADOWS > 0
    PointLightShadow pointLightShadow;
    #endif

    #pragma unroll_loop_start
    for ( int i = 0; i < NUM_POINT_LIGHTS; i ++ ) {

      pointLight = pointLights[ i ];

      // COMPAT: pre-r156 uses a struct GeometricContext
      #if THREE_VRM_THREE_REVISION >= 157
        getPointLightInfo( pointLight, geometryPosition, directLight );
      #else
        getPointLightInfo( pointLight, geometry, directLight );
      #endif

      shadow = 1.0;
      #if defined( USE_SHADOWMAP ) && ( UNROLLED_LOOP_INDEX < NUM_POINT_LIGHT_SHADOWS )
      pointLightShadow = pointLightShadows[ i ];
      // COMPAT: pre-r166
      // r166 introduced shadowIntensity
      #if THREE_VRM_THREE_REVISION >= 166
        shadow = all( bvec2( directLight.visible, receiveShadow ) ) ? getPointShadow( pointShadowMap[ i ], pointLightShadow.shadowMapSize, pointLightShadow.shadowIntensity, pointLightShadow.shadowBias, pointLightShadow.shadowRadius, vPointShadowCoord[ i ], pointLightShadow.shadowCameraNear, pointLightShadow.shadowCameraFar ) : 1.0;
      #else
        shadow = all( bvec2( directLight.visible, receiveShadow ) ) ? getPointShadow( pointShadowMap[ i ], pointLightShadow.shadowMapSize, pointLightShadow.shadowBias, pointLightShadow.shadowRadius, vPointShadowCoord[ i ], pointLightShadow.shadowCameraNear, pointLightShadow.shadowCameraFar ) : 1.0;
      #endif
      #endif

      // COMPAT: pre-r156 uses a struct GeometricContext
      #if THREE_VRM_THREE_REVISION >= 157
        RE_Direct( directLight, geometryPosition, geometryNormal, geometryViewDir, geometryClearcoatNormal, material, shadow, reflectedLight );
      #else
        RE_Direct( directLight, geometry, material, shadow, reflectedLight );
      #endif

    }
    #pragma unroll_loop_end

  #endif

  #if ( NUM_SPOT_LIGHTS > 0 ) && defined( RE_Direct )

    SpotLight spotLight;
    // COMPAT: pre-r144 uses NUM_SPOT_LIGHT_SHADOWS, r144+ uses NUM_SPOT_LIGHT_COORDS
    #if THREE_VRM_THREE_REVISION >= 144
      #if defined( USE_SHADOWMAP ) && NUM_SPOT_LIGHT_COORDS > 0
      SpotLightShadow spotLightShadow;
      #endif
    #elif defined( USE_SHADOWMAP ) && NUM_SPOT_LIGHT_SHADOWS > 0
    SpotLightShadow spotLightShadow;
    #endif

    #pragma unroll_loop_start
    for ( int i = 0; i < NUM_SPOT_LIGHTS; i ++ ) {

      spotLight = spotLights[ i ];

      // COMPAT: pre-r156 uses a struct GeometricContext
      #if THREE_VRM_THREE_REVISION >= 157
        getSpotLightInfo( spotLight, geometryPosition, directLight );
      #else
        getSpotLightInfo( spotLight, geometry, directLight );
      #endif

      shadow = 1.0;
      // COMPAT: pre-r144 uses NUM_SPOT_LIGHT_SHADOWS and vSpotShadowCoord, r144+ uses NUM_SPOT_LIGHT_COORDS and vSpotLightCoord
      // COMPAT: pre-r166 does not have shadowIntensity, r166+ has shadowIntensity
      #if THREE_VRM_THREE_REVISION >= 166
        #if defined( USE_SHADOWMAP ) && ( UNROLLED_LOOP_INDEX < NUM_SPOT_LIGHT_COORDS )
        spotLightShadow = spotLightShadows[ i ];
        shadow = all( bvec2( directLight.visible, receiveShadow ) ) ? getShadow( spotShadowMap[ i ], spotLightShadow.shadowMapSize, spotLightShadow.shadowIntensity, spotLightShadow.shadowBias, spotLightShadow.shadowRadius, vSpotLightCoord[ i ] ) : 1.0;
        #endif
      #elif THREE_VRM_THREE_REVISION >= 144
        #if defined( USE_SHADOWMAP ) && ( UNROLLED_LOOP_INDEX < NUM_SPOT_LIGHT_COORDS )
        spotLightShadow = spotLightShadows[ i ];
        shadow = all( bvec2( directLight.visible, receiveShadow ) ) ? getShadow( spotShadowMap[ i ], spotLightShadow.shadowMapSize, spotLightShadow.shadowBias, spotLightShadow.shadowRadius, vSpotLightCoord[ i ] ) : 1.0;
        #endif
      #elif defined( USE_SHADOWMAP ) && ( UNROLLED_LOOP_INDEX < NUM_SPOT_LIGHT_SHADOWS )
      spotLightShadow = spotLightShadows[ i ];
      shadow = all( bvec2( directLight.visible, receiveShadow ) ) ? getShadow( spotShadowMap[ i ], spotLightShadow.shadowMapSize, spotLightShadow.shadowBias, spotLightShadow.shadowRadius, vSpotShadowCoord[ i ] ) : 1.0;
      #endif

      // COMPAT: pre-r156 uses a struct GeometricContext
      #if THREE_VRM_THREE_REVISION >= 157
        RE_Direct( directLight, geometryPosition, geometryNormal, geometryViewDir, geometryClearcoatNormal, material, shadow, reflectedLight );
      #else
        RE_Direct( directLight, geometry, material, shadow, reflectedLight );
      #endif

    }
    #pragma unroll_loop_end

  #endif

  #if ( NUM_DIR_LIGHTS > 0 ) && defined( RE_Direct )

    DirectionalLight directionalLight;
    #if defined( USE_SHADOWMAP ) && NUM_DIR_LIGHT_SHADOWS > 0
    DirectionalLightShadow directionalLightShadow;
    #endif

    #pragma unroll_loop_start
    for ( int i = 0; i < NUM_DIR_LIGHTS; i ++ ) {

      directionalLight = directionalLights[ i ];

      // COMPAT: pre-r156 uses a struct GeometricContext
      #if THREE_VRM_THREE_REVISION >= 157
        getDirectionalLightInfo( directionalLight, directLight );
      #else
        getDirectionalLightInfo( directionalLight, geometry, directLight );
      #endif

      shadow = 1.0;
      #if defined( USE_SHADOWMAP ) && ( UNROLLED_LOOP_INDEX < NUM_DIR_LIGHT_SHADOWS )
      directionalLightShadow = directionalLightShadows[ i ];
      // COMPAT: pre-r166
      // r166 introduced shadowIntensity
      #if THREE_VRM_THREE_REVISION >= 166
        shadow = all( bvec2( directLight.visible, receiveShadow ) ) ? getShadow( directionalShadowMap[ i ], directionalLightShadow.shadowMapSize, directionalLightShadow.shadowIntensity, directionalLightShadow.shadowBias, directionalLightShadow.shadowRadius, vDirectionalShadowCoord[ i ] ) : 1.0;
      #else
        shadow = all( bvec2( directLight.visible, receiveShadow ) ) ? getShadow( directionalShadowMap[ i ], directionalLightShadow.shadowMapSize, directionalLightShadow.shadowBias, directionalLightShadow.shadowRadius, vDirectionalShadowCoord[ i ] ) : 1.0;
      #endif
      #endif

      // COMPAT: pre-r156 uses a struct GeometricContext
      #if THREE_VRM_THREE_REVISION >= 157
        RE_Direct( directLight, geometryPosition, geometryNormal, geometryViewDir, geometryClearcoatNormal, material, shadow, reflectedLight );
      #else
        RE_Direct( directLight, geometry, material, shadow, reflectedLight );
      #endif

    }
    #pragma unroll_loop_end

  #endif

  // #if ( NUM_RECT_AREA_LIGHTS > 0 ) && defined( RE_Direct_RectArea )

  //   RectAreaLight rectAreaLight;

  //   #pragma unroll_loop_start
  //   for ( int i = 0; i < NUM_RECT_AREA_LIGHTS; i ++ ) {

  //     rectAreaLight = rectAreaLights[ i ];
  //     RE_Direct_RectArea( rectAreaLight, geometry, material, reflectedLight );

  //   }
  //   #pragma unroll_loop_end

  // #endif

  #if defined( RE_IndirectDiffuse )

    vec3 iblIrradiance = vec3( 0.0 );

    vec3 irradiance = getAmbientLightIrradiance( ambientLightColor );

    // COMPAT: pre-r156 uses a struct GeometricContext
    // COMPAT: pre-r156 doesn't have a define USE_LIGHT_PROBES
    #if THREE_VRM_THREE_REVISION >= 157
      #if defined( USE_LIGHT_PROBES )
        irradiance += getLightProbeIrradiance( lightProbe, geometryNormal );
      #endif
    #else
      irradiance += getLightProbeIrradiance( lightProbe, geometry.normal );
    #endif

    #if ( NUM_HEMI_LIGHTS > 0 )

      #pragma unroll_loop_start
      for ( int i = 0; i < NUM_HEMI_LIGHTS; i ++ ) {

        // COMPAT: pre-r156 uses a struct GeometricContext
        #if THREE_VRM_THREE_REVISION >= 157
          irradiance += getHemisphereLightIrradiance( hemisphereLights[ i ], geometryNormal );
        #else
          irradiance += getHemisphereLightIrradiance( hemisphereLights[ i ], geometry.normal );
        #endif

      }
      #pragma unroll_loop_end

    #endif

  #endif

  // #if defined( RE_IndirectSpecular )

  //   vec3 radiance = vec3( 0.0 );
  //   vec3 clearcoatRadiance = vec3( 0.0 );

  // #endif

  #include <lights_fragment_maps>
  #include <lights_fragment_end>

  // modulation
  #include <aomap_fragment>

  vec3 col = reflectedLight.directDiffuse + reflectedLight.indirectDiffuse;

  #ifdef DEBUG_LITSHADERATE
    gl_FragColor = vec4( col, diffuseColor.a );
    postCorrection();
    return;
  #endif

  // -- MToon: rim lighting -----------------------------------------
  vec3 viewDir = normalize( vViewPosition );

  #ifndef PHYSICALLY_CORRECT_LIGHTS
    reflectedLight.directSpecular /= PI;
  #endif
  vec3 rimMix = mix( vec3( 1.0 ), reflectedLight.directSpecular, rimLightingMixFactor );

  vec3 rim = parametricRimColorFactor * pow( saturate( 1.0 - dot( viewDir, normal ) + parametricRimLiftFactor ), parametricRimFresnelPowerFactor );

  #ifdef USE_MATCAPTEXTURE
    {
      vec3 x = normalize( vec3( viewDir.z, 0.0, -viewDir.x ) );
      vec3 y = cross( viewDir, x ); // guaranteed to be normalized
      vec2 sphereUv = 0.5 + 0.5 * vec2( dot( x, normal ), -dot( y, normal ) );
      sphereUv = ( matcapTextureUvTransform * vec3( sphereUv, 1 ) ).xy;
      vec3 matcap = texture2D( matcapTexture, sphereUv ).rgb;
      rim += matcapFactor * matcap;
    }
  #endif

  #ifdef USE_RIMMULTIPLYTEXTURE
    vec2 rimMultiplyTextureUv = ( rimMultiplyTextureUvTransform * vec3( uv, 1 ) ).xy;
    rim *= texture2D( rimMultiplyTexture, rimMultiplyTextureUv ).rgb;
  #endif

  col += rimMix * rim;

  // -- MToon: Emission --------------------------------------------------------
  col += totalEmissiveRadiance;

  // #include <envmap_fragment>

  // -- Almost done! -----------------------------------------------------------
  #if defined( OUTLINE )
    col = outlineColorFactor.rgb * mix( vec3( 1.0 ), col, outlineLightingMixFactor );
  #endif

  #ifdef OPAQUE
    diffuseColor.a = 1.0;
  #endif

  gl_FragColor = vec4( col, diffuseColor.a );
  postCorrection();
}
`, by = { None: "none" }, cu = { None: "none", ScreenCoordinates: "screenCoordinates" }, wy = { 3e3: "", 3001: "srgb" };
function Ro(r) {
  return parseInt(Ir, 10) >= 152 ? r.colorSpace : wy[r.encoding];
}
var Ay = class extends xi {
  constructor(r = {}) {
    var e;
    super({ vertexShader: Ty, fragmentShader: Ey }), this.uvAnimationScrollXSpeedFactor = 0, this.uvAnimationScrollYSpeedFactor = 0, this.uvAnimationRotationSpeedFactor = 0, this.fog = !0, this.normalMapType = la, this._ignoreVertexColor = !0, this._v0CompatShade = !1, this._debugMode = by.None, this._outlineWidthMode = cu.None, this._isOutline = !1, r.transparentWithZWrite && (r.depthWrite = !0), delete r.transparentWithZWrite, r.fog = !0, r.lights = !0, r.clipping = !0, this.uniforms = sd.merge([fe.common, fe.normalmap, fe.emissivemap, fe.fog, fe.lights, { litFactor: { value: new be(1, 1, 1) }, mapUvTransform: { value: new De() }, colorAlpha: { value: 1 }, normalMapUvTransform: { value: new De() }, shadeColorFactor: { value: new be(0, 0, 0) }, shadeMultiplyTexture: { value: null }, shadeMultiplyTextureUvTransform: { value: new De() }, shadingShiftFactor: { value: 0 }, shadingShiftTexture: { value: null }, shadingShiftTextureUvTransform: { value: new De() }, shadingShiftTextureScale: { value: 1 }, shadingToonyFactor: { value: 0.9 }, giEqualizationFactor: { value: 0.9 }, matcapFactor: { value: new be(1, 1, 1) }, matcapTexture: { value: null }, matcapTextureUvTransform: { value: new De() }, parametricRimColorFactor: { value: new be(0, 0, 0) }, rimMultiplyTexture: { value: null }, rimMultiplyTextureUvTransform: { value: new De() }, rimLightingMixFactor: { value: 1 }, parametricRimFresnelPowerFactor: { value: 5 }, parametricRimLiftFactor: { value: 0 }, emissive: { value: new be(0, 0, 0) }, emissiveIntensity: { value: 1 }, emissiveMapUvTransform: { value: new De() }, outlineWidthMultiplyTexture: { value: null }, outlineWidthMultiplyTextureUvTransform: { value: new De() }, outlineWidthFactor: { value: 0 }, outlineColorFactor: { value: new be(0, 0, 0) }, outlineLightingMixFactor: { value: 1 }, uvAnimationMaskTexture: { value: null }, uvAnimationMaskTextureUvTransform: { value: new De() }, uvAnimationScrollXOffset: { value: 0 }, uvAnimationScrollYOffset: { value: 0 }, uvAnimationRotationPhase: { value: 0 } }, (e = r.uniforms) != null ? e : {}]), this.setValues(r), this._uploadUniformsWorkaround(), this.customProgramCacheKey = () => [...Object.entries(this._generateDefines()).map(([t, i]) => `${t}:${i}`), this.matcapTexture ? `matcapTextureColorSpace:${Ro(this.matcapTexture)}` : "", this.shadeMultiplyTexture ? `shadeMultiplyTextureColorSpace:${Ro(this.shadeMultiplyTexture)}` : "", this.rimMultiplyTexture ? `rimMultiplyTextureColorSpace:${Ro(this.rimMultiplyTexture)}` : ""].join(","), this.onBeforeCompile = (t) => {
      const i = parseInt(Ir, 10), n = Object.entries(lu(lu({}, this._generateDefines()), this.defines)).filter(([s, a]) => !!a).map(([s, a]) => `#define ${s} ${a}`).join(`
`) + `
`;
      t.vertexShader = n + t.vertexShader, t.fragmentShader = n + t.fragmentShader, i < 154 && (t.fragmentShader = t.fragmentShader.replace("#include <colorspace_fragment>", "#include <encodings_fragment>"));
    };
  }
  get color() {
    return this.uniforms.litFactor.value;
  }
  set color(r) {
    this.uniforms.litFactor.value = r;
  }
  get map() {
    return this.uniforms.map.value;
  }
  set map(r) {
    this.uniforms.map.value = r;
  }
  get normalMap() {
    return this.uniforms.normalMap.value;
  }
  set normalMap(r) {
    this.uniforms.normalMap.value = r;
  }
  get normalScale() {
    return this.uniforms.normalScale.value;
  }
  set normalScale(r) {
    this.uniforms.normalScale.value = r;
  }
  get emissive() {
    return this.uniforms.emissive.value;
  }
  set emissive(r) {
    this.uniforms.emissive.value = r;
  }
  get emissiveIntensity() {
    return this.uniforms.emissiveIntensity.value;
  }
  set emissiveIntensity(r) {
    this.uniforms.emissiveIntensity.value = r;
  }
  get emissiveMap() {
    return this.uniforms.emissiveMap.value;
  }
  set emissiveMap(r) {
    this.uniforms.emissiveMap.value = r;
  }
  get shadeColorFactor() {
    return this.uniforms.shadeColorFactor.value;
  }
  set shadeColorFactor(r) {
    this.uniforms.shadeColorFactor.value = r;
  }
  get shadeMultiplyTexture() {
    return this.uniforms.shadeMultiplyTexture.value;
  }
  set shadeMultiplyTexture(r) {
    this.uniforms.shadeMultiplyTexture.value = r;
  }
  get shadingShiftFactor() {
    return this.uniforms.shadingShiftFactor.value;
  }
  set shadingShiftFactor(r) {
    this.uniforms.shadingShiftFactor.value = r;
  }
  get shadingShiftTexture() {
    return this.uniforms.shadingShiftTexture.value;
  }
  set shadingShiftTexture(r) {
    this.uniforms.shadingShiftTexture.value = r;
  }
  get shadingShiftTextureScale() {
    return this.uniforms.shadingShiftTextureScale.value;
  }
  set shadingShiftTextureScale(r) {
    this.uniforms.shadingShiftTextureScale.value = r;
  }
  get shadingToonyFactor() {
    return this.uniforms.shadingToonyFactor.value;
  }
  set shadingToonyFactor(r) {
    this.uniforms.shadingToonyFactor.value = r;
  }
  get giEqualizationFactor() {
    return this.uniforms.giEqualizationFactor.value;
  }
  set giEqualizationFactor(r) {
    this.uniforms.giEqualizationFactor.value = r;
  }
  get matcapFactor() {
    return this.uniforms.matcapFactor.value;
  }
  set matcapFactor(r) {
    this.uniforms.matcapFactor.value = r;
  }
  get matcapTexture() {
    return this.uniforms.matcapTexture.value;
  }
  set matcapTexture(r) {
    this.uniforms.matcapTexture.value = r;
  }
  get parametricRimColorFactor() {
    return this.uniforms.parametricRimColorFactor.value;
  }
  set parametricRimColorFactor(r) {
    this.uniforms.parametricRimColorFactor.value = r;
  }
  get rimMultiplyTexture() {
    return this.uniforms.rimMultiplyTexture.value;
  }
  set rimMultiplyTexture(r) {
    this.uniforms.rimMultiplyTexture.value = r;
  }
  get rimLightingMixFactor() {
    return this.uniforms.rimLightingMixFactor.value;
  }
  set rimLightingMixFactor(r) {
    this.uniforms.rimLightingMixFactor.value = r;
  }
  get parametricRimFresnelPowerFactor() {
    return this.uniforms.parametricRimFresnelPowerFactor.value;
  }
  set parametricRimFresnelPowerFactor(r) {
    this.uniforms.parametricRimFresnelPowerFactor.value = r;
  }
  get parametricRimLiftFactor() {
    return this.uniforms.parametricRimLiftFactor.value;
  }
  set parametricRimLiftFactor(r) {
    this.uniforms.parametricRimLiftFactor.value = r;
  }
  get outlineWidthMultiplyTexture() {
    return this.uniforms.outlineWidthMultiplyTexture.value;
  }
  set outlineWidthMultiplyTexture(r) {
    this.uniforms.outlineWidthMultiplyTexture.value = r;
  }
  get outlineWidthFactor() {
    return this.uniforms.outlineWidthFactor.value;
  }
  set outlineWidthFactor(r) {
    this.uniforms.outlineWidthFactor.value = r;
  }
  get outlineColorFactor() {
    return this.uniforms.outlineColorFactor.value;
  }
  set outlineColorFactor(r) {
    this.uniforms.outlineColorFactor.value = r;
  }
  get outlineLightingMixFactor() {
    return this.uniforms.outlineLightingMixFactor.value;
  }
  set outlineLightingMixFactor(r) {
    this.uniforms.outlineLightingMixFactor.value = r;
  }
  get uvAnimationMaskTexture() {
    return this.uniforms.uvAnimationMaskTexture.value;
  }
  set uvAnimationMaskTexture(r) {
    this.uniforms.uvAnimationMaskTexture.value = r;
  }
  get uvAnimationScrollXOffset() {
    return this.uniforms.uvAnimationScrollXOffset.value;
  }
  set uvAnimationScrollXOffset(r) {
    this.uniforms.uvAnimationScrollXOffset.value = r;
  }
  get uvAnimationScrollYOffset() {
    return this.uniforms.uvAnimationScrollYOffset.value;
  }
  set uvAnimationScrollYOffset(r) {
    this.uniforms.uvAnimationScrollYOffset.value = r;
  }
  get uvAnimationRotationPhase() {
    return this.uniforms.uvAnimationRotationPhase.value;
  }
  set uvAnimationRotationPhase(r) {
    this.uniforms.uvAnimationRotationPhase.value = r;
  }
  get ignoreVertexColor() {
    return this._ignoreVertexColor;
  }
  set ignoreVertexColor(r) {
    this._ignoreVertexColor = r, this.needsUpdate = !0;
  }
  get v0CompatShade() {
    return this._v0CompatShade;
  }
  set v0CompatShade(r) {
    this._v0CompatShade = r, this.needsUpdate = !0;
  }
  get debugMode() {
    return this._debugMode;
  }
  set debugMode(r) {
    this._debugMode = r, this.needsUpdate = !0;
  }
  get outlineWidthMode() {
    return this._outlineWidthMode;
  }
  set outlineWidthMode(r) {
    this._outlineWidthMode = r, this.needsUpdate = !0;
  }
  get isOutline() {
    return this._isOutline;
  }
  set isOutline(r) {
    this._isOutline = r, this.needsUpdate = !0;
  }
  get isMToonMaterial() {
    return !0;
  }
  update(r) {
    this._uploadUniformsWorkaround(), this._updateUVAnimation(r);
  }
  copy(r) {
    return super.copy(r), this.map = r.map, this.normalMap = r.normalMap, this.emissiveMap = r.emissiveMap, this.shadeMultiplyTexture = r.shadeMultiplyTexture, this.shadingShiftTexture = r.shadingShiftTexture, this.matcapTexture = r.matcapTexture, this.rimMultiplyTexture = r.rimMultiplyTexture, this.outlineWidthMultiplyTexture = r.outlineWidthMultiplyTexture, this.uvAnimationMaskTexture = r.uvAnimationMaskTexture, this.normalMapType = r.normalMapType, this.uvAnimationScrollXSpeedFactor = r.uvAnimationScrollXSpeedFactor, this.uvAnimationScrollYSpeedFactor = r.uvAnimationScrollYSpeedFactor, this.uvAnimationRotationSpeedFactor = r.uvAnimationRotationSpeedFactor, this.ignoreVertexColor = r.ignoreVertexColor, this.v0CompatShade = r.v0CompatShade, this.debugMode = r.debugMode, this.outlineWidthMode = r.outlineWidthMode, this.isOutline = r.isOutline, this.needsUpdate = !0, this;
  }
  _updateUVAnimation(r) {
    this.uniforms.uvAnimationScrollXOffset.value += r * this.uvAnimationScrollXSpeedFactor, this.uniforms.uvAnimationScrollYOffset.value += r * this.uvAnimationScrollYSpeedFactor, this.uniforms.uvAnimationRotationPhase.value += r * this.uvAnimationRotationSpeedFactor, this.uniforms.alphaTest.value = this.alphaTest, this.uniformsNeedUpdate = !0;
  }
  _uploadUniformsWorkaround() {
    this.uniforms.opacity.value = this.opacity, this._updateTextureMatrix(this.uniforms.map, this.uniforms.mapUvTransform), this._updateTextureMatrix(this.uniforms.normalMap, this.uniforms.normalMapUvTransform), this._updateTextureMatrix(this.uniforms.emissiveMap, this.uniforms.emissiveMapUvTransform), this._updateTextureMatrix(this.uniforms.shadeMultiplyTexture, this.uniforms.shadeMultiplyTextureUvTransform), this._updateTextureMatrix(this.uniforms.shadingShiftTexture, this.uniforms.shadingShiftTextureUvTransform), this._updateTextureMatrix(this.uniforms.matcapTexture, this.uniforms.matcapTextureUvTransform), this._updateTextureMatrix(this.uniforms.rimMultiplyTexture, this.uniforms.rimMultiplyTextureUvTransform), this._updateTextureMatrix(this.uniforms.outlineWidthMultiplyTexture, this.uniforms.outlineWidthMultiplyTextureUvTransform), this._updateTextureMatrix(this.uniforms.uvAnimationMaskTexture, this.uniforms.uvAnimationMaskTextureUvTransform), this.uniformsNeedUpdate = !0;
  }
  _generateDefines() {
    const r = parseInt(Ir, 10), e = this.outlineWidthMultiplyTexture !== null, t = this.map !== null || this.normalMap !== null || this.emissiveMap !== null || this.shadeMultiplyTexture !== null || this.shadingShiftTexture !== null || this.rimMultiplyTexture !== null || this.uvAnimationMaskTexture !== null;
    return { THREE_VRM_THREE_REVISION: r, OUTLINE: this._isOutline, MTOON_USE_UV: e || t, MTOON_UVS_VERTEX_ONLY: e && !t, V0_COMPAT_SHADE: this._v0CompatShade, USE_SHADEMULTIPLYTEXTURE: this.shadeMultiplyTexture !== null, USE_SHADINGSHIFTTEXTURE: this.shadingShiftTexture !== null, USE_MATCAPTEXTURE: this.matcapTexture !== null, USE_RIMMULTIPLYTEXTURE: this.rimMultiplyTexture !== null, USE_OUTLINEWIDTHMULTIPLYTEXTURE: this._isOutline && this.outlineWidthMultiplyTexture !== null, USE_UVANIMATIONMASKTEXTURE: this.uvAnimationMaskTexture !== null, IGNORE_VERTEX_COLOR: this._ignoreVertexColor === !0, DEBUG_NORMAL: this._debugMode === "normal", DEBUG_LITSHADERATE: this._debugMode === "litShadeRate", DEBUG_UV: this._debugMode === "uv", OUTLINE_WIDTH_SCREEN: this._isOutline && this._outlineWidthMode === cu.ScreenCoordinates };
  }
  _updateTextureMatrix(r, e) {
    r.value && (r.value.matrixAutoUpdate && r.value.updateMatrix(), e.value.copy(r.value.matrix));
  }
}, Ry = /* @__PURE__ */ new Set(["1.0", "1.0-beta"]), kd = class ra {
  get name() {
    return ra.EXTENSION_NAME;
  }
  constructor(e, t = {}) {
    var i, n, s, a;
    this.parser = e, this.materialType = (i = t.materialType) != null ? i : Ay, this.renderOrderOffset = (n = t.renderOrderOffset) != null ? n : 0, this.v0CompatShade = (s = t.v0CompatShade) != null ? s : !1, this.debugMode = (a = t.debugMode) != null ? a : "none", this._mToonMaterialSet = /* @__PURE__ */ new Set();
  }
  beforeRoot() {
    return Lr(this, null, function* () {
      this._removeUnlitExtensionIfMToonExists();
    });
  }
  afterRoot(e) {
    return Lr(this, null, function* () {
      e.userData.vrmMToonMaterials = Array.from(this._mToonMaterialSet);
    });
  }
  getMaterialType(e) {
    return this._getMToonExtension(e) ? this.materialType : null;
  }
  extendMaterialParams(e, t) {
    const i = this._getMToonExtension(e);
    return i ? this._extendMaterialParams(i, t) : null;
  }
  loadMesh(e) {
    return Lr(this, null, function* () {
      var t;
      const i = this.parser, n = (t = i.json.meshes) == null ? void 0 : t[e];
      if (n == null) throw new Error(`MToonMaterialLoaderPlugin: Attempt to use meshes[${e}] of glTF but the mesh doesn't exist`);
      const s = n.primitives, a = yield i.loadMesh(e);
      if (s.length === 1) {
        const o = a, l = s[0].material;
        l != null && this._setupPrimitive(o, l);
      } else {
        const o = a;
        for (let l = 0; l < s.length; l++) {
          const c = o.children[l], h = s[l].material;
          h != null && this._setupPrimitive(c, h);
        }
      }
      return a;
    });
  }
  _removeUnlitExtensionIfMToonExists() {
    this.parser.json.materials?.map((t, i) => {
      var n;
      this._getMToonExtension(i) && (n = t.extensions) != null && n.KHR_materials_unlit && delete t.extensions.KHR_materials_unlit;
    });
  }
  _getMToonExtension(e) {
    var t, i;
    const n = (t = this.parser.json.materials) == null ? void 0 : t[e];
    if (n == null) {
      console.warn(`MToonMaterialLoaderPlugin: Attempt to use materials[${e}] of glTF but the material doesn't exist`);
      return;
    }
    const s = (i = n.extensions) == null ? void 0 : i[ra.EXTENSION_NAME];
    if (s == null) return;
    const a = s.specVersion;
    if (!Ry.has(a)) {
      console.warn(`MToonMaterialLoaderPlugin: Unknown ${ra.EXTENSION_NAME} specVersion "${a}"`);
      return;
    }
    return s;
  }
  _extendMaterialParams(e, t) {
    return Lr(this, null, function* () {
      var i;
      delete t.metalness, delete t.roughness;
      const n = new Sy(this.parser, t);
      n.assignPrimitive("transparentWithZWrite", e.transparentWithZWrite), n.assignColor("shadeColorFactor", e.shadeColorFactor), n.assignTexture("shadeMultiplyTexture", e.shadeMultiplyTexture, !0), n.assignPrimitive("shadingShiftFactor", e.shadingShiftFactor), n.assignTexture("shadingShiftTexture", e.shadingShiftTexture, !0), n.assignPrimitive("shadingShiftTextureScale", (i = e.shadingShiftTexture) == null ? void 0 : i.scale), n.assignPrimitive("shadingToonyFactor", e.shadingToonyFactor), n.assignPrimitive("giEqualizationFactor", e.giEqualizationFactor), n.assignColor("matcapFactor", e.matcapFactor), n.assignTexture("matcapTexture", e.matcapTexture, !0), n.assignColor("parametricRimColorFactor", e.parametricRimColorFactor), n.assignTexture("rimMultiplyTexture", e.rimMultiplyTexture, !0), n.assignPrimitive("rimLightingMixFactor", e.rimLightingMixFactor), n.assignPrimitive("parametricRimFresnelPowerFactor", e.parametricRimFresnelPowerFactor), n.assignPrimitive("parametricRimLiftFactor", e.parametricRimLiftFactor), n.assignPrimitive("outlineWidthMode", e.outlineWidthMode), n.assignPrimitive("outlineWidthFactor", e.outlineWidthFactor), n.assignTexture("outlineWidthMultiplyTexture", e.outlineWidthMultiplyTexture, !1), n.assignColor("outlineColorFactor", e.outlineColorFactor), n.assignPrimitive("outlineLightingMixFactor", e.outlineLightingMixFactor), n.assignTexture("uvAnimationMaskTexture", e.uvAnimationMaskTexture, !1), n.assignPrimitive("uvAnimationScrollXSpeedFactor", e.uvAnimationScrollXSpeedFactor), n.assignPrimitive("uvAnimationScrollYSpeedFactor", e.uvAnimationScrollYSpeedFactor), n.assignPrimitive("uvAnimationRotationSpeedFactor", e.uvAnimationRotationSpeedFactor), n.assignPrimitive("v0CompatShade", this.v0CompatShade), n.assignPrimitive("debugMode", this.debugMode), yield n.pending;
    });
  }
  _setupPrimitive(e, t) {
    const i = this._getMToonExtension(t);
    if (i) {
      const n = this._parseRenderOrder(i);
      e.renderOrder = n + this.renderOrderOffset, this._generateOutline(e), this._addToMaterialSet(e);
      return;
    }
  }
  _shouldGenerateOutline(e) {
    return typeof e.outlineWidthMode == "string" && e.outlineWidthMode !== "none" && typeof e.outlineWidthFactor == "number" && e.outlineWidthFactor > 0;
  }
  _generateOutline(e) {
    const t = e.material;
    if (!(t instanceof vi) || !this._shouldGenerateOutline(t)) return;
    e.material = [t];
    const i = t.clone();
    i.name += " (Outline)", i.isOutline = !0, i.side = Gt, e.material.push(i);
    const n = e.geometry, s = n.index ? n.index.count : n.attributes.position.count / 3;
    n.addGroup(0, s, 0), n.addGroup(0, s, 1);
  }
  _addToMaterialSet(e) {
    const t = e.material, i = /* @__PURE__ */ new Set();
    Array.isArray(t) ? t.forEach((n) => i.add(n)) : i.add(t);
    for (const n of i) this._mToonMaterialSet.add(n);
  }
  _parseRenderOrder(e) {
    var t;
    return (e.transparentWithZWrite ? 0 : 19) + ((t = e.renderQueueOffsetNumber) != null ? t : 0);
  }
};
kd.EXTENSION_NAME = "VRMC_materials_mtoon";
var Py = kd, Cy = (r, e, t) => new Promise((i, n) => {
  var s = (l) => {
    try {
      o(t.next(l));
    } catch (c) {
      n(c);
    }
  }, a = (l) => {
    try {
      o(t.throw(l));
    } catch (c) {
      n(c);
    }
  }, o = (l) => l.done ? i(l.value) : Promise.resolve(l.value).then(s, a);
  o((t = t.apply(r, e)).next());
}), zd = class Il {
  get name() {
    return Il.EXTENSION_NAME;
  }
  constructor(e) {
    this.parser = e;
  }
  extendMaterialParams(e, t) {
    return Cy(this, null, function* () {
      const i = this._getHDREmissiveMultiplierExtension(e);
      if (i == null) return;
      console.warn("VRMMaterialsHDREmissiveMultiplierLoaderPlugin: `VRMC_materials_hdr_emissiveMultiplier` is archived. Use `KHR_materials_emissive_strength` instead.");
      const n = i.emissiveMultiplier;
      t.emissiveIntensity = n;
    });
  }
  _getHDREmissiveMultiplierExtension(e) {
    var t, i;
    const n = (t = this.parser.json.materials) == null ? void 0 : t[e];
    if (n == null) {
      console.warn(`VRMMaterialsHDREmissiveMultiplierLoaderPlugin: Attempt to use materials[${e}] of glTF but the material doesn't exist`);
      return;
    }
    const s = (i = n.extensions) == null ? void 0 : i[Il.EXTENSION_NAME];
    if (s != null) return s;
  }
};
zd.EXTENSION_NAME = "VRMC_materials_hdr_emissiveMultiplier";
var Ly = zd, Iy = Object.defineProperty, Ny = Object.defineProperties, Uy = Object.getOwnPropertyDescriptors, hu = Object.getOwnPropertySymbols, Dy = Object.prototype.hasOwnProperty, Oy = Object.prototype.propertyIsEnumerable, uu = (r, e, t) => e in r ? Iy(r, e, { enumerable: !0, configurable: !0, writable: !0, value: t }) : r[e] = t, wi = (r, e) => {
  for (var t in e || (e = {})) Dy.call(e, t) && uu(r, t, e[t]);
  if (hu) for (var t of hu(e)) Oy.call(e, t) && uu(r, t, e[t]);
  return r;
}, du = (r, e) => Ny(r, Uy(e)), Fy = (r, e, t) => new Promise((i, n) => {
  var s = (l) => {
    try {
      o(t.next(l));
    } catch (c) {
      n(c);
    }
  }, a = (l) => {
    try {
      o(t.throw(l));
    } catch (c) {
      n(c);
    }
  }, o = (l) => l.done ? i(l.value) : Promise.resolve(l.value).then(s, a);
  o((t = t.apply(r, e)).next());
});
function en(r) {
  return Math.pow(r, 2.2);
}
var By = class {
  get name() {
    return "VRMMaterialsV0CompatPlugin";
  }
  constructor(r) {
    var e;
    this.parser = r, this._renderQueueMapTransparent = /* @__PURE__ */ new Map(), this._renderQueueMapTransparentZWrite = /* @__PURE__ */ new Map();
    const t = this.parser.json;
    t.extensionsUsed = (e = t.extensionsUsed) != null ? e : [], t.extensionsUsed.indexOf("KHR_texture_transform") === -1 && t.extensionsUsed.push("KHR_texture_transform");
  }
  beforeRoot() {
    return Fy(this, null, function* () {
      var r;
      const e = this.parser.json, t = (r = e.extensions) == null ? void 0 : r.VRM, i = t?.materialProperties;
      i && (this._populateRenderQueueMap(i), i.forEach((n, s) => {
        var a, o;
        const l = (a = e.materials) == null ? void 0 : a[s];
        if (l == null) {
          console.warn(`VRMMaterialsV0CompatPlugin: Attempt to use materials[${s}] of glTF but the material doesn't exist`);
          return;
        }
        if (n.shader === "VRM/MToon") {
          const c = this._parseV0MToonProperties(n, l);
          e.materials[s] = c;
        } else if ((o = n.shader) != null && o.startsWith("VRM/Unlit")) {
          const c = this._parseV0UnlitProperties(n, l);
          e.materials[s] = c;
        } else n.shader === "VRM_USE_GLTFSHADER" || console.warn(`VRMMaterialsV0CompatPlugin: Unknown shader: ${n.shader}`);
      }));
    });
  }
  _parseV0MToonProperties(r, e) {
    var t, i, n, s, a, o, l, c, h, d, u, p, f, v, m, g, E, R, y, S, T, A, x, b, N, O, k, q, B, G, Z, X, te, V, K, ee, Ue, ue, Ye, ke, je, Y, $, le, Oe, xe, Te, He, Ke, Ve, vt, Mt, dt, Je, At;
    const tt = (i = (t = r.keywordMap) == null ? void 0 : t._ALPHABLEND_ON) != null ? i : !1, _t = ((n = r.floatProperties) == null ? void 0 : n._ZWrite) === 1 && tt, C = this._v0ParseRenderQueue(r), xt = (a = (s = r.keywordMap) == null ? void 0 : s._ALPHATEST_ON) != null ? a : !1, Qe = tt ? "BLEND" : xt ? "MASK" : "OPAQUE", w = xt ? (l = (o = r.floatProperties) == null ? void 0 : o._Cutoff) != null ? l : 0.5 : void 0, _ = ((h = (c = r.floatProperties) == null ? void 0 : c._CullMode) != null ? h : 2) === 0, F = this._portTextureTransform(r), W = ((u = (d = r.vectorProperties) == null ? void 0 : d._Color) != null ? u : [1, 1, 1, 1]).map((Vi, Hi) => Hi === 3 ? Vi : en(Vi)), j = (p = r.textureProperties) == null ? void 0 : p._MainTex, oe = j != null ? { index: j, extensions: wi({}, F) } : void 0, he = (v = (f = r.floatProperties) == null ? void 0 : f._BumpScale) != null ? v : 1, D = (m = r.textureProperties) == null ? void 0 : m._BumpMap, se = D != null ? { index: D, scale: he, extensions: wi({}, F) } : void 0, ce = ((E = (g = r.vectorProperties) == null ? void 0 : g._EmissionColor) != null ? E : [0, 0, 0, 1]).map(en), Me = (R = r.textureProperties) == null ? void 0 : R._EmissionMap, ie = Me != null ? { index: Me, extensions: wi({}, F) } : void 0, ge = ((S = (y = r.vectorProperties) == null ? void 0 : y._ShadeColor) != null ? S : [0.97, 0.81, 0.86, 1]).map(en), Se = (T = r.textureProperties) == null ? void 0 : T._ShadeTexture, Pe = Se != null ? { index: Se, extensions: wi({}, F) } : void 0;
    let Ie = (x = (A = r.floatProperties) == null ? void 0 : A._ShadeShift) != null ? x : 0, U = (N = (b = r.floatProperties) == null ? void 0 : b._ShadeToony) != null ? N : 0.9;
    U = mt.lerp(U, 1, 0.5 + 0.5 * Ie), Ie = -Ie - (1 - U);
    const Q = (k = (O = r.floatProperties) == null ? void 0 : O._IndirectLightIntensity) != null ? k : 0.1, re = Q ? 1 - Q : void 0, _e = (q = r.textureProperties) == null ? void 0 : q._SphereAdd, ae = _e != null ? [1, 1, 1] : void 0, J = _e != null ? { index: _e } : void 0, me = (G = (B = r.floatProperties) == null ? void 0 : B._RimLightingMix) != null ? G : 0, pe = (Z = r.textureProperties) == null ? void 0 : Z._RimTexture, Le = pe != null ? { index: pe, extensions: wi({}, F) } : void 0, Ne = ((te = (X = r.vectorProperties) == null ? void 0 : X._RimColor) != null ? te : [0, 0, 0, 1]).map(en), Ut = (K = (V = r.floatProperties) == null ? void 0 : V._RimFresnelPower) != null ? K : 1, Xt = (Ue = (ee = r.floatProperties) == null ? void 0 : ee._RimLift) != null ? Ue : 0, Xe = ["none", "worldCoordinates", "screenCoordinates"][(Ye = (ue = r.floatProperties) == null ? void 0 : ue._OutlineWidthMode) != null ? Ye : 0];
    let Mi = (je = (ke = r.floatProperties) == null ? void 0 : ke._OutlineWidth) != null ? je : 0;
    Mi = 0.01 * Mi;
    const jt = (Y = r.textureProperties) == null ? void 0 : Y._OutlineWidthTexture, qt = jt != null ? { index: jt, extensions: wi({}, F) } : void 0, ti = ((le = ($ = r.vectorProperties) == null ? void 0 : $._OutlineColor) != null ? le : [0, 0, 0]).map(en), gr = ((xe = (Oe = r.floatProperties) == null ? void 0 : Oe._OutlineColorMode) != null ? xe : 0) === 1 ? (He = (Te = r.floatProperties) == null ? void 0 : Te._OutlineLightingMix) != null ? He : 1 : 0, _r = (Ke = r.textureProperties) == null ? void 0 : Ke._UvAnimMaskTexture, ci = _r != null ? { index: _r, extensions: wi({}, F) } : void 0, vr = (vt = (Ve = r.floatProperties) == null ? void 0 : Ve._UvAnimScrollX) != null ? vt : 0;
    let Fi = (dt = (Mt = r.floatProperties) == null ? void 0 : Mt._UvAnimScrollY) != null ? dt : 0;
    Fi != null && (Fi = -Fi);
    const Bi = (At = (Je = r.floatProperties) == null ? void 0 : Je._UvAnimRotation) != null ? At : 0, yi = { specVersion: "1.0", transparentWithZWrite: _t, renderQueueOffsetNumber: C, shadeColorFactor: ge, shadeMultiplyTexture: Pe, shadingShiftFactor: Ie, shadingToonyFactor: U, giEqualizationFactor: re, matcapFactor: ae, matcapTexture: J, rimLightingMixFactor: me, rimMultiplyTexture: Le, parametricRimColorFactor: Ne, parametricRimFresnelPowerFactor: Ut, parametricRimLiftFactor: Xt, outlineWidthMode: Xe, outlineWidthFactor: Mi, outlineWidthMultiplyTexture: qt, outlineColorFactor: ti, outlineLightingMixFactor: gr, uvAnimationMaskTexture: ci, uvAnimationScrollXSpeedFactor: vr, uvAnimationScrollYSpeedFactor: Fi, uvAnimationRotationSpeedFactor: Bi };
    return du(wi({}, e), { pbrMetallicRoughness: { baseColorFactor: W, baseColorTexture: oe }, normalTexture: se, emissiveTexture: ie, emissiveFactor: ce, alphaMode: Qe, alphaCutoff: w, doubleSided: _, extensions: { VRMC_materials_mtoon: yi } });
  }
  _parseV0UnlitProperties(r, e) {
    var t, i, n, s, a;
    const o = r.shader === "VRM/UnlitTransparentZWrite", l = r.shader === "VRM/UnlitTransparent" || o, c = this._v0ParseRenderQueue(r), h = r.shader === "VRM/UnlitCutout", d = l ? "BLEND" : h ? "MASK" : "OPAQUE", u = h ? (i = (t = r.floatProperties) == null ? void 0 : t._Cutoff) != null ? i : 0.5 : void 0, p = this._portTextureTransform(r), f = ((s = (n = r.vectorProperties) == null ? void 0 : n._Color) != null ? s : [1, 1, 1, 1]).map(en), v = (a = r.textureProperties) == null ? void 0 : a._MainTex, m = v != null ? { index: v, extensions: wi({}, p) } : void 0, g = { specVersion: "1.0", transparentWithZWrite: o, renderQueueOffsetNumber: c, shadeColorFactor: f, shadeMultiplyTexture: m };
    return du(wi({}, e), { pbrMetallicRoughness: { baseColorFactor: f, baseColorTexture: m }, alphaMode: d, alphaCutoff: u, extensions: { VRMC_materials_mtoon: g } });
  }
  _portTextureTransform(r) {
    var e, t, i, n, s;
    const a = (e = r.vectorProperties) == null ? void 0 : e._MainTex;
    if (a == null) return {};
    const o = [(t = a?.[0]) != null ? t : 0, (i = a?.[1]) != null ? i : 0], l = [(n = a?.[2]) != null ? n : 1, (s = a?.[3]) != null ? s : 1];
    return o[1] = 1 - l[1] - o[1], { KHR_texture_transform: { offset: o, scale: l } };
  }
  _v0ParseRenderQueue(r) {
    var e, t;
    const i = r.shader === "VRM/UnlitTransparentZWrite", n = ((e = r.keywordMap) == null ? void 0 : e._ALPHABLEND_ON) != null || r.shader === "VRM/UnlitTransparent" || i, s = ((t = r.floatProperties) == null ? void 0 : t._ZWrite) === 1 || i;
    let a = 0;
    if (n) {
      const o = r.renderQueue;
      o != null && (s ? a = this._renderQueueMapTransparentZWrite.get(o) : a = this._renderQueueMapTransparent.get(o));
    }
    return a;
  }
  _populateRenderQueueMap(r) {
    const e = /* @__PURE__ */ new Set(), t = /* @__PURE__ */ new Set();
    r.forEach((i) => {
      var n, s;
      const a = i.shader === "VRM/UnlitTransparentZWrite", o = ((n = i.keywordMap) == null ? void 0 : n._ALPHABLEND_ON) != null || i.shader === "VRM/UnlitTransparent" || a, l = ((s = i.floatProperties) == null ? void 0 : s._ZWrite) === 1 || a;
      if (o) {
        const c = i.renderQueue;
        c != null && (l ? t.add(c) : e.add(c));
      }
    }), e.size > 10 && console.warn(`VRMMaterialsV0CompatPlugin: This VRM uses ${e.size} render queues for Transparent materials while VRM 1.0 only supports up to 10 render queues. The model might not be rendered correctly.`), t.size > 10 && console.warn(`VRMMaterialsV0CompatPlugin: This VRM uses ${t.size} render queues for TransparentZWrite materials while VRM 1.0 only supports up to 10 render queues. The model might not be rendered correctly.`), Array.from(e).sort().forEach((i, n) => {
      const s = Math.min(Math.max(n - e.size + 1, -9), 0);
      this._renderQueueMapTransparent.set(i, s);
    }), Array.from(t).sort().forEach((i, n) => {
      const s = Math.min(Math.max(n, 0), 9);
      this._renderQueueMapTransparentZWrite.set(i, s);
    });
  }
}, pu = (r, e, t) => new Promise((i, n) => {
  var s = (l) => {
    try {
      o(t.next(l));
    } catch (c) {
      n(c);
    }
  }, a = (l) => {
    try {
      o(t.throw(l));
    } catch (c) {
      n(c);
    }
  }, o = (l) => l.done ? i(l.value) : Promise.resolve(l.value).then(s, a);
  o((t = t.apply(r, e)).next());
}), hr = new P(), Po = class extends Qt {
  constructor(r) {
    super(), this._attrPosition = new nt(new Float32Array([0, 0, 0, 0, 0, 0]), 3), this._attrPosition.setUsage(Wp);
    const e = new yt();
    e.setAttribute("position", this._attrPosition);
    const t = new Dr({ color: 16711935, depthTest: !1, depthWrite: !1 });
    this._line = new ya(e, t), this.add(this._line), this.constraint = r;
  }
  updateMatrixWorld(r) {
    hr.setFromMatrixPosition(this.constraint.destination.matrixWorld), this._attrPosition.setXYZ(0, hr.x, hr.y, hr.z), this.constraint.source && hr.setFromMatrixPosition(this.constraint.source.matrixWorld), this._attrPosition.setXYZ(1, hr.x, hr.y, hr.z), this._attrPosition.needsUpdate = !0, super.updateMatrixWorld(r);
  }
};
function fu(r, e) {
  return e.set(r.elements[12], r.elements[13], r.elements[14]);
}
var Vy = new P(), Hy = new P();
function ky(r, e) {
  return r.decompose(Vy, e, Hy), e;
}
function ga(r) {
  return r.invert ? r.invert() : r.inverse(), r;
}
var ec = class {
  constructor(r, e) {
    this.destination = r, this.source = e, this.weight = 1;
  }
}, zy = new P(), Gy = new P(), Wy = new P(), Xy = new Ce(), jy = new Ce(), qy = new Ce(), Yy = class extends ec {
  get aimAxis() {
    return this._aimAxis;
  }
  set aimAxis(r) {
    this._aimAxis = r, this._v3AimAxis.set(r === "PositiveX" ? 1 : r === "NegativeX" ? -1 : 0, r === "PositiveY" ? 1 : r === "NegativeY" ? -1 : 0, r === "PositiveZ" ? 1 : r === "NegativeZ" ? -1 : 0);
  }
  get dependencies() {
    const r = /* @__PURE__ */ new Set([this.source]);
    return this.destination.parent && r.add(this.destination.parent), r;
  }
  constructor(r, e) {
    super(r, e), this._aimAxis = "PositiveX", this._v3AimAxis = new P(1, 0, 0), this._dstRestQuat = new Ce();
  }
  setInitState() {
    this._dstRestQuat.copy(this.destination.quaternion);
  }
  update() {
    this.destination.updateWorldMatrix(!0, !1), this.source.updateWorldMatrix(!0, !1);
    const r = Xy.identity(), e = jy.identity();
    this.destination.parent && (ky(this.destination.parent.matrixWorld, r), ga(e.copy(r)));
    const t = zy.copy(this._v3AimAxis).applyQuaternion(this._dstRestQuat).applyQuaternion(r), i = fu(this.source.matrixWorld, Gy).sub(fu(this.destination.matrixWorld, Wy)).normalize(), n = qy.setFromUnitVectors(t, i).premultiply(e).multiply(r).multiply(this._dstRestQuat);
    this.destination.quaternion.copy(this._dstRestQuat).slerp(n, this.weight);
  }
};
function Ky(r, e) {
  const t = [r];
  let i = r.parent;
  for (; i !== null; ) t.unshift(i), i = i.parent;
  t.forEach((n) => {
    e(n);
  });
}
var Zy = class {
  constructor() {
    this._constraints = /* @__PURE__ */ new Set(), this._objectConstraintsMap = /* @__PURE__ */ new Map();
  }
  get constraints() {
    return this._constraints;
  }
  addConstraint(r) {
    this._constraints.add(r);
    let e = this._objectConstraintsMap.get(r.destination);
    e == null && (e = /* @__PURE__ */ new Set(), this._objectConstraintsMap.set(r.destination, e)), e.add(r);
  }
  deleteConstraint(r) {
    this._constraints.delete(r), this._objectConstraintsMap.get(r.destination).delete(r);
  }
  setInitState() {
    const r = /* @__PURE__ */ new Set(), e = /* @__PURE__ */ new Set();
    for (const t of this._constraints) this._processConstraint(t, r, e, (i) => i.setInitState());
  }
  update() {
    const r = /* @__PURE__ */ new Set(), e = /* @__PURE__ */ new Set();
    for (const t of this._constraints) this._processConstraint(t, r, e, (i) => i.update());
  }
  _processConstraint(r, e, t, i) {
    if (t.has(r)) return;
    if (e.has(r)) throw new Error("VRMNodeConstraintManager: Circular dependency detected while updating constraints");
    e.add(r);
    const n = r.dependencies;
    for (const s of n) Ky(s, (a) => {
      const o = this._objectConstraintsMap.get(a);
      if (o) for (const l of o) this._processConstraint(l, e, t, i);
    });
    i(r), t.add(r);
  }
}, $y = new Ce(), Qy = new Ce(), Jy = class extends ec {
  get dependencies() {
    return /* @__PURE__ */ new Set([this.source]);
  }
  constructor(r, e) {
    super(r, e), this._dstRestQuat = new Ce(), this._invSrcRestQuat = new Ce();
  }
  setInitState() {
    this._dstRestQuat.copy(this.destination.quaternion), ga(this._invSrcRestQuat.copy(this.source.quaternion));
  }
  update() {
    const r = $y.copy(this._invSrcRestQuat).multiply(this.source.quaternion), e = Qy.copy(this._dstRestQuat).multiply(r);
    this.destination.quaternion.copy(this._dstRestQuat).slerp(e, this.weight);
  }
}, eS = new P(), tS = new Ce(), iS = new Ce(), rS = class extends ec {
  get rollAxis() {
    return this._rollAxis;
  }
  set rollAxis(r) {
    this._rollAxis = r, this._v3RollAxis.set(r === "X" ? 1 : 0, r === "Y" ? 1 : 0, r === "Z" ? 1 : 0);
  }
  get dependencies() {
    return /* @__PURE__ */ new Set([this.source]);
  }
  constructor(r, e) {
    super(r, e), this._rollAxis = "X", this._v3RollAxis = new P(1, 0, 0), this._dstRestQuat = new Ce(), this._invDstRestQuat = new Ce(), this._invSrcRestQuatMulDstRestQuat = new Ce();
  }
  setInitState() {
    this._dstRestQuat.copy(this.destination.quaternion), ga(this._invDstRestQuat.copy(this._dstRestQuat)), ga(this._invSrcRestQuatMulDstRestQuat.copy(this.source.quaternion)).multiply(this._dstRestQuat);
  }
  update() {
    const r = tS.copy(this._invDstRestQuat).multiply(this.source.quaternion).multiply(this._invSrcRestQuatMulDstRestQuat), e = eS.copy(this._v3RollAxis).applyQuaternion(r), t = iS.setFromUnitVectors(e, this._v3RollAxis).premultiply(this._dstRestQuat).multiply(r);
    this.destination.quaternion.copy(this._dstRestQuat).slerp(t, this.weight);
  }
}, nS = /* @__PURE__ */ new Set(["1.0", "1.0-beta"]), Gd = class $n {
  get name() {
    return $n.EXTENSION_NAME;
  }
  constructor(e, t) {
    this.parser = e, this.helperRoot = t?.helperRoot;
  }
  afterRoot(e) {
    return pu(this, null, function* () {
      e.userData.vrmNodeConstraintManager = yield this._import(e);
    });
  }
  _import(e) {
    return pu(this, null, function* () {
      var t;
      const i = this.parser.json;
      if (((t = i.extensionsUsed) == null ? void 0 : t.indexOf($n.EXTENSION_NAME)) === -1) return null;
      const n = new Zy(), s = yield this.parser.getDependencies("node");
      return s.forEach((a, o) => {
        var l;
        const c = i.nodes[o], h = (l = c?.extensions) == null ? void 0 : l[$n.EXTENSION_NAME];
        if (h == null) return;
        const d = h.specVersion;
        if (!nS.has(d)) {
          console.warn(`VRMNodeConstraintLoaderPlugin: Unknown ${$n.EXTENSION_NAME} specVersion "${d}"`);
          return;
        }
        const u = h.constraint;
        if (u.roll != null) {
          const p = this._importRollConstraint(a, s, u.roll);
          n.addConstraint(p);
        } else if (u.aim != null) {
          const p = this._importAimConstraint(a, s, u.aim);
          n.addConstraint(p);
        } else if (u.rotation != null) {
          const p = this._importRotationConstraint(a, s, u.rotation);
          n.addConstraint(p);
        }
      }), e.scene.updateMatrixWorld(), n.setInitState(), n;
    });
  }
  _importRollConstraint(e, t, i) {
    const { source: n, rollAxis: s, weight: a } = i, o = t[n], l = new rS(e, o);
    if (s != null && (l.rollAxis = s), a != null && (l.weight = a), this.helperRoot) {
      const c = new Po(l);
      this.helperRoot.add(c);
    }
    return l;
  }
  _importAimConstraint(e, t, i) {
    const { source: n, aimAxis: s, weight: a } = i, o = t[n], l = new Yy(e, o);
    if (s != null && (l.aimAxis = s), a != null && (l.weight = a), this.helperRoot) {
      const c = new Po(l);
      this.helperRoot.add(c);
    }
    return l;
  }
  _importRotationConstraint(e, t, i) {
    const { source: n, weight: s } = i, a = t[n], o = new Jy(e, a);
    if (s != null && (o.weight = s), this.helperRoot) {
      const l = new Po(o);
      this.helperRoot.add(l);
    }
    return o;
  }
};
Gd.EXTENSION_NAME = "VRMC_node_constraint";
var sS = Gd, js = (r, e, t) => new Promise((i, n) => {
  var s = (l) => {
    try {
      o(t.next(l));
    } catch (c) {
      n(c);
    }
  }, a = (l) => {
    try {
      o(t.throw(l));
    } catch (c) {
      n(c);
    }
  }, o = (l) => l.done ? i(l.value) : Promise.resolve(l.value).then(s, a);
  o((t = t.apply(r, e)).next());
}), tc = class {
}, Co = new P(), tn = new P(), Wd = class extends tc {
  get type() {
    return "capsule";
  }
  constructor(r) {
    var e, t, i, n;
    super(), this.offset = (e = r?.offset) != null ? e : new P(0, 0, 0), this.tail = (t = r?.tail) != null ? t : new P(0, 0, 0), this.radius = (i = r?.radius) != null ? i : 0, this.inside = (n = r?.inside) != null ? n : !1;
  }
  calculateCollision(r, e, t, i) {
    Co.setFromMatrixPosition(r), tn.subVectors(this.tail, this.offset).applyMatrix4(r), tn.sub(Co);
    const n = tn.lengthSq();
    i.copy(e).sub(Co);
    const s = tn.dot(i);
    s <= 0 || (n <= s || tn.multiplyScalar(s / n), i.sub(tn));
    const a = i.length(), o = this.inside ? this.radius - t - a : a - t - this.radius;
    return o < 0 && (i.multiplyScalar(1 / a), this.inside && i.negate()), o;
  }
}, Lo = new P(), mu = new De(), Xd = class extends tc {
  get type() {
    return "plane";
  }
  constructor(r) {
    var e, t;
    super(), this.offset = (e = r?.offset) != null ? e : new P(0, 0, 0), this.normal = (t = r?.normal) != null ? t : new P(0, 0, 1);
  }
  calculateCollision(r, e, t, i) {
    i.setFromMatrixPosition(r), i.negate().add(e), mu.getNormalMatrix(r), Lo.copy(this.normal).applyNormalMatrix(mu).normalize();
    const n = i.dot(Lo) - t;
    return i.copy(Lo), n;
  }
}, aS = new P(), jd = class extends tc {
  get type() {
    return "sphere";
  }
  constructor(r) {
    var e, t, i;
    super(), this.offset = (e = r?.offset) != null ? e : new P(0, 0, 0), this.radius = (t = r?.radius) != null ? t : 0, this.inside = (i = r?.inside) != null ? i : !1;
  }
  calculateCollision(r, e, t, i) {
    i.subVectors(e, aS.setFromMatrixPosition(r));
    const n = i.length(), s = this.inside ? this.radius - t - n : n - t - this.radius;
    return s < 0 && (i.multiplyScalar(1 / n), this.inside && i.negate()), s;
  }
}, Ai = new P(), oS = class extends yt {
  constructor(r) {
    super(), this.worldScale = 1, this._currentRadius = 0, this._currentOffset = new P(), this._currentTail = new P(), this._shape = r, this._attrPos = new nt(new Float32Array(396), 3), this.setAttribute("position", this._attrPos), this._attrIndex = new nt(new Uint16Array(264), 1), this.setIndex(this._attrIndex), this._buildIndex(), this.update();
  }
  update() {
    let r = !1;
    const e = this._shape.radius / this.worldScale;
    this._currentRadius !== e && (this._currentRadius = e, r = !0), this._currentOffset.equals(this._shape.offset) || (this._currentOffset.copy(this._shape.offset), r = !0);
    const t = Ai.copy(this._shape.tail).divideScalar(this.worldScale);
    this._currentTail.distanceToSquared(t) > 1e-10 && (this._currentTail.copy(t), r = !0), r && this._buildPosition();
  }
  _buildPosition() {
    Ai.copy(this._currentTail).sub(this._currentOffset);
    const r = Ai.length() / this._currentRadius;
    for (let i = 0; i <= 16; i++) {
      const n = i / 16 * Math.PI;
      this._attrPos.setXYZ(i, -Math.sin(n), -Math.cos(n), 0), this._attrPos.setXYZ(17 + i, r + Math.sin(n), Math.cos(n), 0), this._attrPos.setXYZ(34 + i, -Math.sin(n), 0, -Math.cos(n)), this._attrPos.setXYZ(51 + i, r + Math.sin(n), 0, Math.cos(n));
    }
    for (let i = 0; i < 32; i++) {
      const n = i / 16 * Math.PI;
      this._attrPos.setXYZ(68 + i, 0, Math.sin(n), Math.cos(n)), this._attrPos.setXYZ(100 + i, r, Math.sin(n), Math.cos(n));
    }
    const e = Math.atan2(Ai.y, Math.sqrt(Ai.x * Ai.x + Ai.z * Ai.z)), t = -Math.atan2(Ai.z, Ai.x);
    this.rotateZ(e), this.rotateY(t), this.scale(this._currentRadius, this._currentRadius, this._currentRadius), this.translate(this._currentOffset.x, this._currentOffset.y, this._currentOffset.z), this._attrPos.needsUpdate = !0;
  }
  _buildIndex() {
    for (let r = 0; r < 34; r++) {
      const e = (r + 1) % 34;
      this._attrIndex.setXY(r * 2, r, e), this._attrIndex.setXY(68 + r * 2, 34 + r, 34 + e);
    }
    for (let r = 0; r < 32; r++) {
      const e = (r + 1) % 32;
      this._attrIndex.setXY(136 + r * 2, 68 + r, 68 + e), this._attrIndex.setXY(200 + r * 2, 100 + r, 100 + e);
    }
    this._attrIndex.needsUpdate = !0;
  }
}, lS = class extends yt {
  constructor(r) {
    super(), this.worldScale = 1, this._currentOffset = new P(), this._currentNormal = new P(), this._shape = r, this._attrPos = new nt(new Float32Array(6 * 3), 3), this.setAttribute("position", this._attrPos), this._attrIndex = new nt(new Uint16Array(10), 1), this.setIndex(this._attrIndex), this._buildIndex(), this.update();
  }
  update() {
    let r = !1;
    this._currentOffset.equals(this._shape.offset) || (this._currentOffset.copy(this._shape.offset), r = !0), this._currentNormal.equals(this._shape.normal) || (this._currentNormal.copy(this._shape.normal), r = !0), r && this._buildPosition();
  }
  _buildPosition() {
    this._attrPos.setXYZ(0, -0.5, -0.5, 0), this._attrPos.setXYZ(1, 0.5, -0.5, 0), this._attrPos.setXYZ(2, 0.5, 0.5, 0), this._attrPos.setXYZ(3, -0.5, 0.5, 0), this._attrPos.setXYZ(4, 0, 0, 0), this._attrPos.setXYZ(5, 0, 0, 0.25), this.translate(this._currentOffset.x, this._currentOffset.y, this._currentOffset.z), this.lookAt(this._currentNormal), this._attrPos.needsUpdate = !0;
  }
  _buildIndex() {
    this._attrIndex.setXY(0, 0, 1), this._attrIndex.setXY(2, 1, 2), this._attrIndex.setXY(4, 2, 3), this._attrIndex.setXY(6, 3, 0), this._attrIndex.setXY(8, 4, 5), this._attrIndex.needsUpdate = !0;
  }
}, cS = class extends yt {
  constructor(r) {
    super(), this.worldScale = 1, this._currentRadius = 0, this._currentOffset = new P(), this._shape = r, this._attrPos = new nt(new Float32Array(32 * 3 * 3), 3), this.setAttribute("position", this._attrPos), this._attrIndex = new nt(new Uint16Array(64 * 3), 1), this.setIndex(this._attrIndex), this._buildIndex(), this.update();
  }
  update() {
    let r = !1;
    const e = this._shape.radius / this.worldScale;
    this._currentRadius !== e && (this._currentRadius = e, r = !0), this._currentOffset.equals(this._shape.offset) || (this._currentOffset.copy(this._shape.offset), r = !0), r && this._buildPosition();
  }
  _buildPosition() {
    for (let r = 0; r < 32; r++) {
      const e = r / 16 * Math.PI;
      this._attrPos.setXYZ(r, Math.cos(e), Math.sin(e), 0), this._attrPos.setXYZ(32 + r, 0, Math.cos(e), Math.sin(e)), this._attrPos.setXYZ(64 + r, Math.sin(e), 0, Math.cos(e));
    }
    this.scale(this._currentRadius, this._currentRadius, this._currentRadius), this.translate(this._currentOffset.x, this._currentOffset.y, this._currentOffset.z), this._attrPos.needsUpdate = !0;
  }
  _buildIndex() {
    for (let r = 0; r < 32; r++) {
      const e = (r + 1) % 32;
      this._attrIndex.setXY(r * 2, r, e), this._attrIndex.setXY(64 + r * 2, 32 + r, 32 + e), this._attrIndex.setXY(128 + r * 2, 64 + r, 64 + e);
    }
    this._attrIndex.needsUpdate = !0;
  }
}, hS = new P(), Io = class extends Qt {
  constructor(r) {
    if (super(), this.matrixAutoUpdate = !1, this.collider = r, this.collider.shape instanceof jd) this._geometry = new cS(this.collider.shape);
    else if (this.collider.shape instanceof Wd) this._geometry = new oS(this.collider.shape);
    else if (this.collider.shape instanceof Xd) this._geometry = new lS(this.collider.shape);
    else throw new Error("VRMSpringBoneColliderHelper: Unknown collider shape type detected");
    const e = new Dr({ color: 16711935, depthTest: !1, depthWrite: !1 });
    this._line = new ds(this._geometry, e), this.add(this._line);
  }
  dispose() {
    this._geometry.dispose();
  }
  updateMatrixWorld(r) {
    this.collider.updateWorldMatrix(!0, !1), this.matrix.copy(this.collider.matrixWorld);
    const e = this.matrix.elements;
    this._geometry.worldScale = hS.set(e[0], e[1], e[2]).length(), this._geometry.update(), super.updateMatrixWorld(r);
  }
}, uS = class extends yt {
  constructor(r) {
    super(), this.worldScale = 1, this._currentRadius = 0, this._currentTail = new P(), this._springBone = r, this._attrPos = new nt(new Float32Array(294), 3), this.setAttribute("position", this._attrPos), this._attrIndex = new nt(new Uint16Array(194), 1), this.setIndex(this._attrIndex), this._buildIndex(), this.update();
  }
  update() {
    let r = !1;
    const e = this._springBone.settings.hitRadius / this.worldScale;
    this._currentRadius !== e && (this._currentRadius = e, r = !0), this._currentTail.equals(this._springBone.initialLocalChildPosition) || (this._currentTail.copy(this._springBone.initialLocalChildPosition), r = !0), r && this._buildPosition();
  }
  _buildPosition() {
    for (let r = 0; r < 32; r++) {
      const e = r / 16 * Math.PI;
      this._attrPos.setXYZ(r, Math.cos(e), Math.sin(e), 0), this._attrPos.setXYZ(32 + r, 0, Math.cos(e), Math.sin(e)), this._attrPos.setXYZ(64 + r, Math.sin(e), 0, Math.cos(e));
    }
    this.scale(this._currentRadius, this._currentRadius, this._currentRadius), this.translate(this._currentTail.x, this._currentTail.y, this._currentTail.z), this._attrPos.setXYZ(96, 0, 0, 0), this._attrPos.setXYZ(97, this._currentTail.x, this._currentTail.y, this._currentTail.z), this._attrPos.needsUpdate = !0;
  }
  _buildIndex() {
    for (let r = 0; r < 32; r++) {
      const e = (r + 1) % 32;
      this._attrIndex.setXY(r * 2, r, e), this._attrIndex.setXY(64 + r * 2, 32 + r, 32 + e), this._attrIndex.setXY(128 + r * 2, 64 + r, 64 + e);
    }
    this._attrIndex.setXY(192, 96, 97), this._attrIndex.needsUpdate = !0;
  }
}, dS = new P(), pS = class extends Qt {
  constructor(r) {
    super(), this.matrixAutoUpdate = !1, this.springBone = r, this._geometry = new uS(this.springBone);
    const e = new Dr({ color: 16776960, depthTest: !1, depthWrite: !1 });
    this._line = new ds(this._geometry, e), this.add(this._line);
  }
  dispose() {
    this._geometry.dispose();
  }
  updateMatrixWorld(r) {
    this.springBone.bone.updateWorldMatrix(!0, !1), this.matrix.copy(this.springBone.bone.matrixWorld);
    const e = this.matrix.elements;
    this._geometry.worldScale = dS.set(e[0], e[1], e[2]).length(), this._geometry.update(), super.updateMatrixWorld(r);
  }
}, No = class extends at {
  constructor(r) {
    super(), this.colliderMatrix = new Fe(), this.shape = r;
  }
  updateWorldMatrix(r, e) {
    super.updateWorldMatrix(r, e), fS(this.colliderMatrix, this.matrixWorld, this.shape.offset);
  }
};
function fS(r, e, t) {
  const i = e.elements;
  r.copy(e), t && (r.elements[12] = i[0] * t.x + i[4] * t.y + i[8] * t.z + i[12], r.elements[13] = i[1] * t.x + i[5] * t.y + i[9] * t.z + i[13], r.elements[14] = i[2] * t.x + i[6] * t.y + i[10] * t.z + i[14]);
}
var mS = new Fe();
function gS(r) {
  return r.invert ? r.invert() : r.getInverse(mS.copy(r)), r;
}
var _S = class {
  constructor(r) {
    this._inverseCache = new Fe(), this._shouldUpdateInverse = !0, this.matrix = r;
    const e = { set: (t, i, n) => (this._shouldUpdateInverse = !0, t[i] = n, !0) };
    this._originalElements = r.elements, r.elements = new Proxy(r.elements, e);
  }
  get inverse() {
    return this._shouldUpdateInverse && (gS(this._inverseCache.copy(this.matrix)), this._shouldUpdateInverse = !1), this._inverseCache;
  }
  revert() {
    this.matrix.elements = this._originalElements;
  }
}, Uo = new Fe(), rn = new P(), Wn = new P(), Xn = new P(), jn = new P(), vS = new Fe(), xS = class {
  constructor(r, e, t = {}, i = []) {
    this._currentTail = new P(), this._prevTail = new P(), this._boneAxis = new P(), this._worldSpaceBoneLength = 0, this._center = null, this._initialLocalMatrix = new Fe(), this._initialLocalRotation = new Ce(), this._initialLocalChildPosition = new P();
    var n, s, a, o, l, c;
    this.bone = r, this.bone.matrixAutoUpdate = !1, this.child = e, this.settings = { hitRadius: (n = t.hitRadius) != null ? n : 0, stiffness: (s = t.stiffness) != null ? s : 1, gravityPower: (a = t.gravityPower) != null ? a : 0, gravityDir: (l = (o = t.gravityDir) == null ? void 0 : o.clone()) != null ? l : new P(0, -1, 0), dragForce: (c = t.dragForce) != null ? c : 0.4 }, this.colliderGroups = i;
  }
  get dependencies() {
    const r = /* @__PURE__ */ new Set(), e = this.bone.parent;
    e && r.add(e);
    for (let t = 0; t < this.colliderGroups.length; t++) for (let i = 0; i < this.colliderGroups[t].colliders.length; i++) r.add(this.colliderGroups[t].colliders[i]);
    return r;
  }
  get center() {
    return this._center;
  }
  set center(r) {
    var e;
    (e = this._center) != null && e.userData.inverseCacheProxy && (this._center.userData.inverseCacheProxy.revert(), delete this._center.userData.inverseCacheProxy), this._center = r, this._center && (this._center.userData.inverseCacheProxy || (this._center.userData.inverseCacheProxy = new _S(this._center.matrixWorld)));
  }
  get initialLocalChildPosition() {
    return this._initialLocalChildPosition;
  }
  get _parentMatrixWorld() {
    return this.bone.parent ? this.bone.parent.matrixWorld : Uo;
  }
  setInitState() {
    this._initialLocalMatrix.copy(this.bone.matrix), this._initialLocalRotation.copy(this.bone.quaternion), this.child ? this._initialLocalChildPosition.copy(this.child.position) : this._initialLocalChildPosition.copy(this.bone.position).normalize().multiplyScalar(0.07);
    const r = this._getMatrixWorldToCenter();
    this.bone.localToWorld(this._currentTail.copy(this._initialLocalChildPosition)).applyMatrix4(r), this._prevTail.copy(this._currentTail), this._boneAxis.copy(this._initialLocalChildPosition).normalize();
  }
  reset() {
    this.bone.quaternion.copy(this._initialLocalRotation), this.bone.updateMatrix(), this.bone.matrixWorld.multiplyMatrices(this._parentMatrixWorld, this.bone.matrix);
    const r = this._getMatrixWorldToCenter();
    this.bone.localToWorld(this._currentTail.copy(this._initialLocalChildPosition)).applyMatrix4(r), this._prevTail.copy(this._currentTail);
  }
  update(r) {
    if (r <= 0) return;
    this._calcWorldSpaceBoneLength();
    const e = Wn.copy(this._boneAxis).transformDirection(this._initialLocalMatrix).transformDirection(this._parentMatrixWorld);
    jn.copy(this._currentTail).add(rn.subVectors(this._currentTail, this._prevTail).multiplyScalar(1 - this.settings.dragForce)).applyMatrix4(this._getMatrixCenterToWorld()).addScaledVector(e, this.settings.stiffness * r).addScaledVector(this.settings.gravityDir, this.settings.gravityPower * r), Xn.setFromMatrixPosition(this.bone.matrixWorld), jn.sub(Xn).normalize().multiplyScalar(this._worldSpaceBoneLength).add(Xn), this._collision(jn), this._prevTail.copy(this._currentTail), this._currentTail.copy(jn).applyMatrix4(this._getMatrixWorldToCenter());
    const t = vS.multiplyMatrices(this._parentMatrixWorld, this._initialLocalMatrix).invert();
    this.bone.quaternion.setFromUnitVectors(this._boneAxis, rn.copy(jn).applyMatrix4(t).normalize()).premultiply(this._initialLocalRotation), this.bone.updateMatrix(), this.bone.matrixWorld.multiplyMatrices(this._parentMatrixWorld, this.bone.matrix);
  }
  _collision(r) {
    for (let e = 0; e < this.colliderGroups.length; e++) for (let t = 0; t < this.colliderGroups[e].colliders.length; t++) {
      const i = this.colliderGroups[e].colliders[t], n = i.shape.calculateCollision(i.colliderMatrix, r, this.settings.hitRadius, rn);
      if (n < 0) {
        r.addScaledVector(rn, -n), r.sub(Xn);
        const s = r.length();
        r.multiplyScalar(this._worldSpaceBoneLength / s).add(Xn);
      }
    }
  }
  _calcWorldSpaceBoneLength() {
    rn.setFromMatrixPosition(this.bone.matrixWorld), this.child ? Wn.setFromMatrixPosition(this.child.matrixWorld) : (Wn.copy(this._initialLocalChildPosition), Wn.applyMatrix4(this.bone.matrixWorld)), this._worldSpaceBoneLength = rn.sub(Wn).length();
  }
  _getMatrixCenterToWorld() {
    return this._center ? this._center.matrixWorld : Uo;
  }
  _getMatrixWorldToCenter() {
    return this._center ? this._center.userData.inverseCacheProxy.inverse : Uo;
  }
};
function MS(r, e) {
  const t = [];
  let i = r;
  for (; i !== null; ) t.unshift(i), i = i.parent;
  t.forEach((n) => {
    e(n);
  });
}
function Nl(r, e) {
  r.children.forEach((t) => {
    e(t) || Nl(t, e);
  });
}
function yS(r) {
  var e;
  const t = /* @__PURE__ */ new Map();
  for (const i of r) {
    let n = i;
    do {
      const s = ((e = t.get(n)) != null ? e : 0) + 1;
      if (s === r.size) return n;
      t.set(n, s), n = n.parent;
    } while (n !== null);
  }
  return null;
}
var gu = class {
  constructor() {
    this._joints = /* @__PURE__ */ new Set(), this._sortedJoints = [], this._hasWarnedCircularDependency = !1, this._ancestors = [], this._objectSpringBonesMap = /* @__PURE__ */ new Map(), this._isSortedJointsDirty = !1, this._relevantChildrenUpdated = this._relevantChildrenUpdated.bind(this);
  }
  get joints() {
    return this._joints;
  }
  get springBones() {
    return console.warn("VRMSpringBoneManager: springBones is deprecated. use joints instead."), this._joints;
  }
  get colliderGroups() {
    const r = /* @__PURE__ */ new Set();
    return this._joints.forEach((e) => {
      e.colliderGroups.forEach((t) => {
        r.add(t);
      });
    }), Array.from(r);
  }
  get colliders() {
    const r = /* @__PURE__ */ new Set();
    return this.colliderGroups.forEach((e) => {
      e.colliders.forEach((t) => {
        r.add(t);
      });
    }), Array.from(r);
  }
  addJoint(r) {
    this._joints.add(r);
    let e = this._objectSpringBonesMap.get(r.bone);
    e == null && (e = /* @__PURE__ */ new Set(), this._objectSpringBonesMap.set(r.bone, e)), e.add(r), this._isSortedJointsDirty = !0;
  }
  addSpringBone(r) {
    console.warn("VRMSpringBoneManager: addSpringBone() is deprecated. use addJoint() instead."), this.addJoint(r);
  }
  deleteJoint(r) {
    this._joints.delete(r), this._objectSpringBonesMap.get(r.bone).delete(r), this._isSortedJointsDirty = !0;
  }
  deleteSpringBone(r) {
    console.warn("VRMSpringBoneManager: deleteSpringBone() is deprecated. use deleteJoint() instead."), this.deleteJoint(r);
  }
  setInitState() {
    this._sortJoints();
    for (let r = 0; r < this._sortedJoints.length; r++) {
      const e = this._sortedJoints[r];
      e.bone.updateMatrix(), e.bone.updateWorldMatrix(!1, !1), e.setInitState();
    }
  }
  reset() {
    this._sortJoints();
    for (let r = 0; r < this._sortedJoints.length; r++) {
      const e = this._sortedJoints[r];
      e.bone.updateMatrix(), e.bone.updateWorldMatrix(!1, !1), e.reset();
    }
  }
  update(r) {
    this._sortJoints();
    for (let e = 0; e < this._ancestors.length; e++) this._ancestors[e].updateWorldMatrix(e === 0, !1);
    for (let e = 0; e < this._sortedJoints.length; e++) {
      const t = this._sortedJoints[e];
      t.bone.updateMatrix(), t.bone.updateWorldMatrix(!1, !1), t.update(r), Nl(t.bone, this._relevantChildrenUpdated);
    }
  }
  _sortJoints() {
    if (!this._isSortedJointsDirty) return;
    const r = [], e = /* @__PURE__ */ new Set(), t = /* @__PURE__ */ new Set(), i = /* @__PURE__ */ new Set();
    for (const s of this._joints) this._insertJointSort(s, e, t, r, i);
    this._sortedJoints = r;
    const n = yS(i);
    this._ancestors = [], n && (this._ancestors.push(n), Nl(n, (s) => {
      var a, o;
      return ((o = (a = this._objectSpringBonesMap.get(s)) == null ? void 0 : a.size) != null ? o : 0) > 0 ? !0 : (this._ancestors.push(s), !1);
    })), this._isSortedJointsDirty = !1;
  }
  _insertJointSort(r, e, t, i, n) {
    if (t.has(r)) return;
    if (e.has(r)) {
      this._hasWarnedCircularDependency || (console.warn("VRMSpringBoneManager: Circular dependency detected"), this._hasWarnedCircularDependency = !0);
      return;
    }
    e.add(r);
    const s = r.dependencies;
    for (const a of s) {
      let o = !1, l = null;
      MS(a, (c) => {
        const h = this._objectSpringBonesMap.get(c);
        if (h) for (const d of h) o = !0, this._insertJointSort(d, e, t, i, n);
        else o || (l = c);
      }), l && n.add(l);
    }
    i.push(r), t.add(r);
  }
  _relevantChildrenUpdated(r) {
    var e, t;
    return ((t = (e = this._objectSpringBonesMap.get(r)) == null ? void 0 : e.size) != null ? t : 0) > 0 ? !0 : (r.updateWorldMatrix(!1, !1), !1);
  }
}, _u = "VRMC_springBone_extended_collider", SS = /* @__PURE__ */ new Set(["1.0", "1.0-beta"]), TS = /* @__PURE__ */ new Set(["1.0"]), qd = class cn {
  get name() {
    return cn.EXTENSION_NAME;
  }
  constructor(e, t) {
    var i;
    this.parser = e, this.jointHelperRoot = t?.jointHelperRoot, this.colliderHelperRoot = t?.colliderHelperRoot, this.useExtendedColliders = (i = t?.useExtendedColliders) != null ? i : !0;
  }
  afterRoot(e) {
    return js(this, null, function* () {
      e.userData.vrmSpringBoneManager = yield this._import(e);
    });
  }
  _import(e) {
    return js(this, null, function* () {
      const t = yield this._v1Import(e);
      return t ?? (yield this._v0Import(e)) ?? null;
    });
  }
  _v1Import(e) {
    return js(this, null, function* () {
      var t, i, n, s, a;
      const o = e.parser.json;
      if (((t = o.extensionsUsed) == null ? void 0 : t.indexOf(cn.EXTENSION_NAME)) === -1) return null;
      const l = new gu(), c = yield e.parser.getDependencies("node"), h = (i = o.extensions) == null ? void 0 : i[cn.EXTENSION_NAME];
      if (!h) return null;
      const d = h.specVersion;
      if (!SS.has(d)) return console.warn(`VRMSpringBoneLoaderPlugin: Unknown ${cn.EXTENSION_NAME} specVersion "${d}"`), null;
      const u = (n = h.colliders) == null ? void 0 : n.map((f, v) => {
        var m, g, E, R, y, S, T, A, x, b, N, O, k, q, B;
        const G = c[f.node];
        if (G == null) return console.warn(`VRMSpringBoneLoaderPlugin: The collider #${v} attempted to reference a node #${f.node} but not found. Skipping the collider`), null;
        const Z = f.shape, X = (m = f.extensions) == null ? void 0 : m[_u];
        if (this.useExtendedColliders && X != null) {
          const te = X.specVersion;
          if (!TS.has(te)) console.warn(`VRMSpringBoneLoaderPlugin: Unknown ${_u} specVersion "${te}". Fallbacking to the ${cn.EXTENSION_NAME} definition`);
          else {
            const V = X.shape;
            if (V.sphere) return this._importSphereCollider(G, { offset: new P().fromArray((g = V.sphere.offset) != null ? g : [0, 0, 0]), radius: (E = V.sphere.radius) != null ? E : 0, inside: (R = V.sphere.inside) != null ? R : !1 });
            if (V.capsule) return this._importCapsuleCollider(G, { offset: new P().fromArray((y = V.capsule.offset) != null ? y : [0, 0, 0]), radius: (S = V.capsule.radius) != null ? S : 0, tail: new P().fromArray((T = V.capsule.tail) != null ? T : [0, 0, 0]), inside: (A = V.capsule.inside) != null ? A : !1 });
            if (V.plane) return this._importPlaneCollider(G, { offset: new P().fromArray((x = V.plane.offset) != null ? x : [0, 0, 0]), normal: new P().fromArray((b = V.plane.normal) != null ? b : [0, 0, 1]) });
          }
        }
        if (Z.sphere) return this._importSphereCollider(G, { offset: new P().fromArray((N = Z.sphere.offset) != null ? N : [0, 0, 0]), radius: (O = Z.sphere.radius) != null ? O : 0, inside: !1 });
        if (Z.capsule) return this._importCapsuleCollider(G, { offset: new P().fromArray((k = Z.capsule.offset) != null ? k : [0, 0, 0]), radius: (q = Z.capsule.radius) != null ? q : 0, tail: new P().fromArray((B = Z.capsule.tail) != null ? B : [0, 0, 0]), inside: !1 });
        console.warn(`VRMSpringBoneLoaderPlugin: The collider #${v} has no valid shape. Skipping the collider`);
      }), p = (s = h.colliderGroups) == null ? void 0 : s.map((f, v) => {
        var m;
        return { colliders: ((m = f.colliders) != null ? m : []).map((g) => u?.[g] ?? (console.warn(`VRMSpringBoneLoaderPlugin: The collider group #${v} attempted to reference a collider #${g} but not found. Skipping the collider`), null)).filter((g) => g != null), name: f.name };
      });
      return (a = h.springs) == null || a.forEach((f, v) => {
        var m;
        const g = f.joints, E = (m = f.colliderGroups) == null ? void 0 : m.map((S) => p?.[S] ?? (console.warn(`VRMSpringBoneLoaderPlugin: The spring #${v} attempted to reference a collider group #${S} but not found. Skipping the collider group`), null)).filter((S) => S != null), R = f.center != null ? c[f.center] : void 0;
        let y;
        g.forEach((S) => {
          if (y) {
            const T = y.node, A = c[T], x = S.node, b = c[x], N = { hitRadius: y.hitRadius, dragForce: y.dragForce, gravityPower: y.gravityPower, stiffness: y.stiffness, gravityDir: y.gravityDir != null ? new P().fromArray(y.gravityDir) : void 0 }, O = this._importJoint(A, b, N, E);
            R && (O.center = R), l.addJoint(O);
          }
          y = S;
        });
      }), l.setInitState(), l;
    });
  }
  _v0Import(e) {
    return js(this, null, function* () {
      var t, i, n;
      const s = e.parser.json;
      if (((t = s.extensionsUsed) == null ? void 0 : t.indexOf("VRM")) === -1) return null;
      const a = (i = s.extensions) == null ? void 0 : i.VRM, o = a?.secondaryAnimation;
      if (!o) return null;
      const l = o?.boneGroups;
      if (!l) return null;
      const c = new gu(), h = yield e.parser.getDependencies("node"), d = (n = o.colliderGroups) == null ? void 0 : n.map((u, p) => {
        var f;
        const v = h[u.node];
        return v == null ? (console.warn(`VRMSpringBoneLoaderPlugin: The collider group #${p} attempted to reference a node #${u.node} but not found. Skipping the collider group`), null) : { colliders: ((f = u.colliders) != null ? f : []).map((m, g) => {
          var E, R, y;
          const S = new P(0, 0, 0);
          return m.offset && S.set((E = m.offset.x) != null ? E : 0, (R = m.offset.y) != null ? R : 0, m.offset.z ? -m.offset.z : 0), this._importSphereCollider(v, { offset: S, radius: (y = m.radius) != null ? y : 0, inside: !1 });
        }) };
      });
      return l?.forEach((u, p) => {
        const f = u.bones;
        f && f.forEach((v) => {
          var m, g, E, R;
          const y = h[v];
          if (y == null) {
            console.warn(`VRMSpringBoneLoaderPlugin: The spring bone group #${p} attempted to reference a node #${v} but not found. Skipping the node`);
            return;
          }
          const S = new P();
          u.gravityDir ? S.set((m = u.gravityDir.x) != null ? m : 0, (g = u.gravityDir.y) != null ? g : 0, (E = u.gravityDir.z) != null ? E : 0) : S.set(0, -1, 0);
          const T = u.center != null ? h[u.center] : void 0, A = { hitRadius: u.hitRadius, dragForce: u.dragForce, gravityPower: u.gravityPower, stiffness: u.stiffiness, gravityDir: S }, x = (R = u.colliderGroups) == null ? void 0 : R.map((b) => d?.[b] ?? (console.warn(`VRMSpringBoneLoaderPlugin: The spring #${p} attempted to reference a collider group #${b} but not found. Skipping the collider group`), null)).filter((b) => b != null);
          y.traverse((b) => {
            var N;
            const O = (N = b.children[0]) != null ? N : null, k = this._importJoint(b, O, A, x);
            T && (k.center = T), c.addJoint(k);
          });
        });
      }), e.scene.updateMatrixWorld(), c.setInitState(), c;
    });
  }
  _importJoint(e, t, i, n) {
    const s = new xS(e, t, i, n);
    if (this.jointHelperRoot) {
      const a = new pS(s);
      this.jointHelperRoot.add(a), a.renderOrder = this.jointHelperRoot.renderOrder;
    }
    return s;
  }
  _importSphereCollider(e, t) {
    const i = new jd(t), n = new No(i);
    if (e.add(n), this.colliderHelperRoot) {
      const s = new Io(n);
      this.colliderHelperRoot.add(s), s.renderOrder = this.colliderHelperRoot.renderOrder;
    }
    return n;
  }
  _importCapsuleCollider(e, t) {
    const i = new Wd(t), n = new No(i);
    if (e.add(n), this.colliderHelperRoot) {
      const s = new Io(n);
      this.colliderHelperRoot.add(s), s.renderOrder = this.colliderHelperRoot.renderOrder;
    }
    return n;
  }
  _importPlaneCollider(e, t) {
    const i = new Xd(t), n = new No(i);
    if (e.add(n), this.colliderHelperRoot) {
      const s = new Io(n);
      this.colliderHelperRoot.add(s), s.renderOrder = this.colliderHelperRoot.renderOrder;
    }
    return n;
  }
};
qd.EXTENSION_NAME = "VRMC_springBone";
var ES = qd, bS = class {
  get name() {
    return "VRMLoaderPlugin";
  }
  constructor(r, e) {
    var t, i, n, s, a, o, l, c, h, d;
    this.parser = r;
    const u = e?.helperRoot, p = e?.autoUpdateHumanBones;
    this.expressionPlugin = (t = e?.expressionPlugin) != null ? t : new kM(r), this.firstPersonPlugin = (i = e?.firstPersonPlugin) != null ? i : new GM(r), this.humanoidPlugin = (n = e?.humanoidPlugin) != null ? n : new ZM(r, { helperRoot: u, autoUpdateHumanBones: p }), this.lookAtPlugin = (s = e?.lookAtPlugin) != null ? s : new uy(r, { helperRoot: u }), this.metaPlugin = (a = e?.metaPlugin) != null ? a : new fy(r), this.mtoonMaterialPlugin = (o = e?.mtoonMaterialPlugin) != null ? o : new Py(r), this.materialsHDREmissiveMultiplierPlugin = (l = e?.materialsHDREmissiveMultiplierPlugin) != null ? l : new Ly(r), this.materialsV0CompatPlugin = (c = e?.materialsV0CompatPlugin) != null ? c : new By(r), this.springBonePlugin = (h = e?.springBonePlugin) != null ? h : new ES(r, { colliderHelperRoot: u, jointHelperRoot: u }), this.nodeConstraintPlugin = (d = e?.nodeConstraintPlugin) != null ? d : new sS(r, { helperRoot: u });
  }
  beforeRoot() {
    return Gs(this, null, function* () {
      yield this.materialsV0CompatPlugin.beforeRoot(), yield this.mtoonMaterialPlugin.beforeRoot();
    });
  }
  loadMesh(r) {
    return Gs(this, null, function* () {
      return yield this.mtoonMaterialPlugin.loadMesh(r);
    });
  }
  getMaterialType(r) {
    return this.mtoonMaterialPlugin.getMaterialType(r) ?? null;
  }
  extendMaterialParams(r, e) {
    return Gs(this, null, function* () {
      yield this.materialsHDREmissiveMultiplierPlugin.extendMaterialParams(r, e), yield this.mtoonMaterialPlugin.extendMaterialParams(r, e);
    });
  }
  afterRoot(r) {
    return Gs(this, null, function* () {
      yield this.metaPlugin.afterRoot(r), yield this.humanoidPlugin.afterRoot(r), yield this.expressionPlugin.afterRoot(r), yield this.lookAtPlugin.afterRoot(r), yield this.firstPersonPlugin.afterRoot(r), yield this.springBonePlugin.afterRoot(r), yield this.nodeConstraintPlugin.afterRoot(r), yield this.mtoonMaterialPlugin.afterRoot(r);
      const e = r.userData.vrmMeta, t = r.userData.vrmHumanoid;
      if (e && t) {
        const i = new gy({ scene: r.scene, expressionManager: r.userData.vrmExpressionManager, firstPerson: r.userData.vrmFirstPerson, humanoid: t, lookAt: r.userData.vrmLookAt, meta: e, materials: r.userData.vrmMToonMaterials, springBoneManager: r.userData.vrmSpringBoneManager, nodeConstraintManager: r.userData.vrmNodeConstraintManager });
        r.userData.vrm = i;
      }
    });
  }
};
function wS(r) {
  const e = /* @__PURE__ */ new Set();
  return r.traverse((t) => {
    if (!t.isMesh) return;
    const i = t;
    e.add(i);
  }), e;
}
function vu(r, e, t) {
  if (e.size === 1) {
    const s = e.values().next().value;
    if (s.weight === 1) return r[s.index];
  }
  const i = new Float32Array(r[0].count * 3);
  let n = 0;
  if (t) n = 1;
  else for (const s of e) n += s.weight;
  for (const s of e) {
    const a = r[s.index], o = s.weight / n;
    for (let l = 0; l < a.count; l++) i[l * 3 + 0] += a.getX(l) * o, i[l * 3 + 1] += a.getY(l) * o, i[l * 3 + 2] += a.getZ(l) * o;
  }
  return new nt(i, 3);
}
function AS(r) {
  var e;
  const t = wS(r.scene), i = /* @__PURE__ */ new Map(), n = (e = r.expressionManager) == null ? void 0 : e.expressionMap;
  if (n != null) for (const [s, a] of Object.entries(n)) {
    const o = /* @__PURE__ */ new Set();
    for (const l of a.binds) if (l instanceof ma) {
      if (l.weight !== 0) for (const c of l.primitives) {
        let h = i.get(c);
        h == null && (h = /* @__PURE__ */ new Map(), i.set(c, h));
        let d = h.get(s);
        d == null && (d = /* @__PURE__ */ new Set(), h.set(s, d)), d.add(l);
      }
      o.add(l);
    }
    for (const l of o) a.deleteBind(l);
  }
  for (const s of t) {
    const a = i.get(s);
    if (a == null) continue;
    const o = s.geometry.morphAttributes;
    s.geometry.morphAttributes = {};
    const l = s.geometry.clone();
    s.geometry = l;
    const c = l.morphTargetsRelative, h = o.position != null, d = o.normal != null, u = {}, p = {}, f = [];
    if (h || d) {
      h && (u.position = []), d && (u.normal = []);
      let v = 0;
      for (const [m, g] of a) h && (u.position[v] = vu(o.position, g, c)), d && (u.normal[v] = vu(o.normal, g, c)), n?.[m].addBind(new ma({ index: v, weight: 1, primitives: [s] })), p[m] = v, f.push(0), v++;
    }
    l.morphAttributes = u, s.morphTargetDictionary = p, s.morphTargetInfluences = f;
  }
}
function _a(r, e, t) {
  if (r.getComponent) return r.getComponent(e, t);
  {
    let i = r.array[e * r.itemSize + t];
    return r.normalized && (i = mt.denormalize(i, r.array)), i;
  }
}
function Yd(r, e, t, i) {
  r.setComponent ? r.setComponent(e, t, i) : (r.normalized && (i = mt.normalize(i, r.array)), r.array[e * r.itemSize + t] = i);
}
function RS(r) {
  var e;
  const t = PS(r), i = /* @__PURE__ */ new Set();
  for (const d of t) i.has(d.geometry) && (d.geometry = DS(d.geometry)), i.add(d.geometry);
  const n = /* @__PURE__ */ new Map();
  for (const d of i) {
    const u = d.getAttribute("skinIndex"), p = (e = n.get(u)) != null ? e : /* @__PURE__ */ new Map();
    n.set(u, p);
    const f = d.getAttribute("skinWeight"), v = CS(u, f);
    p.set(f, v);
  }
  const s = /* @__PURE__ */ new Map();
  for (const d of t) {
    const u = LS(d, n);
    s.set(d, u);
  }
  const a = [];
  for (const [d, u] of s) {
    let p = !1;
    for (const f of a) if (IS(u, f.boneInverseMap)) {
      p = !0, f.meshes.add(d);
      for (const [v, m] of u) f.boneInverseMap.set(v, m);
      break;
    }
    p || a.push({ boneInverseMap: u, meshes: /* @__PURE__ */ new Set([d]) });
  }
  const o = /* @__PURE__ */ new Map(), l = new Do(), c = new Do(), h = new Do();
  for (const d of a) {
    const { boneInverseMap: u, meshes: p } = d, f = Array.from(u.keys()), v = Array.from(u.values()), m = new Sn(f, v), g = c.getOrCreate(m);
    for (const E of p) {
      const R = E.geometry.getAttribute("skinIndex"), y = l.getOrCreate(R), S = E.skeleton.bones, T = S.map((b) => h.getOrCreate(b)).join(","), A = `${y};${g};${T}`;
      let x = o.get(A);
      x == null && (x = R.clone(), NS(x, S, f), o.set(A, x)), E.geometry.setAttribute("skinIndex", x);
    }
    for (const E of p) E.bind(m, new Fe());
  }
}
function PS(r) {
  const e = /* @__PURE__ */ new Set();
  return r.traverse((t) => {
    if (!t.isSkinnedMesh) return;
    const i = t;
    e.add(i);
  }), e;
}
function CS(r, e) {
  const t = /* @__PURE__ */ new Set();
  for (let i = 0; i < r.count; i++) for (let n = 0; n < r.itemSize; n++) {
    const s = _a(r, i, n);
    _a(e, i, n) !== 0 && t.add(s);
  }
  return t;
}
function LS(r, e) {
  const t = /* @__PURE__ */ new Map(), i = r.skeleton, n = r.geometry, s = n.getAttribute("skinIndex"), a = n.getAttribute("skinWeight"), o = e.get(s), l = o?.get(a);
  if (!l) throw new Error("Unreachable. attributeUsedIndexSetMap does not know the skin index attribute or the skin weight attribute.");
  for (const c of l) t.set(i.bones[c], i.boneInverses[c]);
  return t;
}
function IS(r, e) {
  for (const [t, i] of r.entries()) {
    const n = e.get(t);
    if (n != null && !US(i, n)) return !1;
  }
  return !0;
}
function NS(r, e, t) {
  const i = /* @__PURE__ */ new Map();
  for (const s of e) i.set(s, i.size);
  const n = /* @__PURE__ */ new Map();
  for (const [s, a] of t.entries()) {
    const o = i.get(a);
    n.set(o, s);
  }
  for (let s = 0; s < r.count; s++) for (let a = 0; a < r.itemSize; a++) {
    const o = _a(r, s, a), l = n.get(o);
    Yd(r, s, a, l);
  }
  r.needsUpdate = !0;
}
function US(r, e, t) {
  if (t = t || 1e-4, r.elements.length != e.elements.length) return !1;
  for (let i = 0, n = r.elements.length; i < n; i++) if (Math.abs(r.elements[i] - e.elements[i]) > t) return !1;
  return !0;
}
var Do = class {
  constructor() {
    this._objectIndexMap = /* @__PURE__ */ new Map(), this._index = 0;
  }
  get(r) {
    return this._objectIndexMap.get(r);
  }
  getOrCreate(r) {
    let e = this._objectIndexMap.get(r);
    return e == null && (e = this._index, this._objectIndexMap.set(r, e), this._index++), e;
  }
};
function DS(r) {
  var e, t, i, n;
  const s = new yt();
  s.name = r.name, s.setIndex(r.index);
  for (const [a, o] of Object.entries(r.attributes)) s.setAttribute(a, o);
  for (const [a, o] of Object.entries(r.morphAttributes)) {
    const l = a;
    s.morphAttributes[l] = o.concat();
  }
  s.morphTargetsRelative = r.morphTargetsRelative, s.groups = [];
  for (const a of r.groups) s.addGroup(a.start, a.count, a.materialIndex);
  return s.boundingSphere = (t = (e = r.boundingSphere) == null ? void 0 : e.clone()) != null ? t : null, s.boundingBox = (n = (i = r.boundingBox) == null ? void 0 : i.clone()) != null ? n : null, s.drawRange.start = r.drawRange.start, s.drawRange.count = r.drawRange.count, s.userData = r.userData, s;
}
function xu(r) {
  if (Object.values(r).forEach((e) => {
    e?.isTexture && e.dispose();
  }), r.isShaderMaterial) {
    const e = r.uniforms;
    e && Object.values(e).forEach((t) => {
      const i = t.value;
      i?.isTexture && i.dispose();
    });
  }
  r.dispose();
}
function OS(r) {
  const e = r.geometry;
  e && e.dispose();
  const t = r.skeleton;
  t && t.dispose();
  const i = r.material;
  i && (Array.isArray(i) ? i.forEach((n) => xu(n)) : i && xu(i));
}
function FS(r) {
  r.traverse(OS);
}
function BS(r, e) {
  var t, i;
  console.warn("VRMUtils.removeUnnecessaryJoints: removeUnnecessaryJoints is deprecated. Use combineSkeletons instead. combineSkeletons contributes more to the performance improvement. This function will be removed in the next major version.");
  const n = (t = e?.experimentalSameBoneCounts) != null ? t : !1, s = [];
  r.traverse((l) => {
    l.type === "SkinnedMesh" && s.push(l);
  });
  const a = /* @__PURE__ */ new Map();
  let o = 0;
  for (const l of s) {
    const c = l.geometry.getAttribute("skinIndex");
    if (a.has(c)) continue;
    const h = /* @__PURE__ */ new Map(), d = /* @__PURE__ */ new Map();
    for (let u = 0; u < c.count; u++) for (let p = 0; p < c.itemSize; p++) {
      const f = _a(c, u, p);
      let v = h.get(f);
      v == null && (v = h.size, h.set(f, v), d.set(v, f)), Yd(c, u, p, v);
    }
    c.needsUpdate = !0, a.set(c, d), o = Math.max(o, h.size);
  }
  for (const l of s) {
    const c = l.geometry.getAttribute("skinIndex"), h = a.get(c), d = [], u = [], p = n ? o : h.size;
    for (let v = 0; v < p; v++) {
      const m = (i = h.get(v)) != null ? i : 0;
      d.push(l.skeleton.bones[m]), u.push(l.skeleton.boneInverses[m]);
    }
    const f = new Sn(d, u);
    l.bind(f, new Fe());
  }
}
function VS(r, e) {
  const t = r.position.count, i = new Array(t);
  let n = 0;
  const s = e.array;
  for (let a = 0; a < s.length; a++) {
    const o = s[a];
    i[o] || (i[o] = !0, n++);
  }
  return { isVertexUsed: i, vertexCount: t, verticesUsed: n };
}
function HS(r) {
  const e = [], t = [];
  let i = 0;
  for (let n = 0; n < r.length; n++) if (r[n]) {
    const s = i++;
    e[n] = s, t[s] = n;
  }
  return { originalIndexNewIndexMap: e, newIndexOriginalIndexMap: t };
}
function kS(r, e) {
  var t, i, n, s;
  e.name = r.name, e.morphTargetsRelative = r.morphTargetsRelative, r.groups.forEach((a) => {
    e.addGroup(a.start, a.count, a.materialIndex);
  }), e.boundingBox = (i = (t = r.boundingBox) == null ? void 0 : t.clone()) != null ? i : null, e.boundingSphere = (s = (n = r.boundingSphere) == null ? void 0 : n.clone()) != null ? s : null, e.setDrawRange(r.drawRange.start, r.drawRange.count), e.userData = r.userData;
}
function zS(r, e, t) {
  const i = e.array, n = new i.constructor(i.length);
  for (let s = 0; s < i.length; s++) {
    const a = i[s];
    n[s] = t[a];
  }
  r.setIndex(new nt(n, e.itemSize, e.normalized));
}
function va(r, e, t) {
  const i = r.constructor, n = new i(e.length * t);
  let s = !0;
  for (let a = 0; a < e.length; a++) {
    const o = e[a] * t, l = a * t;
    for (let c = 0; c < t; c++) {
      const h = r[o + c];
      n[l + c] = h, s = s && h === 0;
    }
  }
  return [n, s];
}
function GS(r) {
  var e;
  const t = /* @__PURE__ */ new Map(), i = [];
  for (const [n, s] of Object.entries(r)) if (s.isInterleavedBufferAttribute) {
    const a = s, o = a.data, l = (e = t.get(o)) != null ? e : [];
    t.set(o, l), l.push([n, a]);
  } else {
    const a = s;
    i.push([n, a]);
  }
  return [t, i];
}
function WS(r, e, t) {
  const [i, n] = GS(e);
  for (const [s, a] of i) {
    const o = s.array, { stride: l } = s, [c] = va(o, t, l), h = new ql(c, l);
    h.setUsage(s.usage);
    for (const [d, u] of a) {
      const { itemSize: p, offset: f, normalized: v } = u, m = new us(h, p, f, v);
      r.setAttribute(d, m);
    }
  }
  for (const [s, a] of n) {
    const o = a.array, { itemSize: l, normalized: c } = a, [h] = va(o, t, l);
    r.setAttribute(s, new nt(h, l, c));
  }
}
function XS(r) {
  var e;
  const t = /* @__PURE__ */ new Map(), i = [];
  for (const [n, s] of Object.entries(r)) {
    const a = n;
    for (let o = 0; o < s.length; o++) {
      const l = s[o];
      if (l.isInterleavedBufferAttribute) {
        const c = l, h = c.data, d = (e = t.get(h)) != null ? e : [];
        t.set(h, d), d.push([a, o, c]);
      } else {
        const c = l;
        i.push([a, o, c]);
      }
    }
  }
  return [t, i];
}
function jS(r, e, t) {
  var i, n;
  let s = !0;
  const [a, o] = XS(e), l = {};
  for (const [c, h] of a) {
    const d = c.array, { stride: u } = c, [p, f] = va(d, t, u);
    s = s && f;
    const v = new ql(p, u);
    v.setUsage(c.usage);
    for (const [m, g, E] of h) {
      const { itemSize: R, offset: y, normalized: S } = E, T = new us(v, R, y, S);
      (i = l[m]) != null || (l[m] = []), l[m][g] = T;
    }
  }
  for (const [c, h, d] of o) {
    const u = d, p = u.array, { itemSize: f, normalized: v } = u, [m, g] = va(p, t, f);
    s = s && g, (n = l[c]) != null || (l[c] = []), l[c][h] = new nt(m, f, v);
  }
  r.morphAttributes = s ? {} : l;
}
function qS(r) {
  const e = /* @__PURE__ */ new Map();
  r.traverse((t) => {
    if (!t.isMesh) return;
    const i = t, n = i.geometry, s = n.index;
    if (s == null) return;
    const a = e.get(n);
    if (a != null) {
      i.geometry = a;
      return;
    }
    const { isVertexUsed: o, vertexCount: l, verticesUsed: c } = VS(n.attributes, s);
    if (c === l) return;
    const { originalIndexNewIndexMap: h, newIndexOriginalIndexMap: d } = HS(o), u = new yt();
    kS(n, u), e.set(n, u), zS(u, s, h), WS(u, n.attributes, d), jS(u, n.morphAttributes, d), i.geometry = u;
  }), Array.from(e.keys()).forEach((t) => {
    t.dispose();
  });
}
function YS(r) {
  var e;
  ((e = r.meta) == null ? void 0 : e.metaVersion) === "0" && (r.scene.rotation.y = Math.PI);
}
var Pr = class {
  constructor() {
  }
};
Pr.combineMorphs = AS, Pr.combineSkeletons = RS, Pr.deepDispose = FS, Pr.removeUnnecessaryJoints = BS, Pr.removeUnnecessaryVertices = qS, Pr.rotateVRM0 = YS;
const Ul = Pr;
function KS(r, e) {
  const t = new cd();
  e && (t.onError = e), t.setURLModifier((n) => {
    if (n !== r && !n.startsWith("blob:") && !n.startsWith("data:")) throw new Error("External model resources are unsupported");
    return n;
  });
  const i = new Sd(t);
  return i.register((n) => new bS(n)), i;
}
function ZS(r) {
  const e = r.userData.vrm;
  if (!e) throw Ul.deepDispose(r.scene), new Error("File is not a VRM avatar");
  return Ul.rotateVRM0(e), e;
}
async function $S(r, e) {
  const t = await KS("", e).parseAsync(r, "");
  return ZS(t);
}
function Mu(r) {
  Ul.deepDispose(r.scene);
}
const QS = 0.9, JS = 1.6, eT = 0.5, tT = 0.3;
function iT({ headY: r, aspect: e, fovDeg: t }) {
  const i = Number.isFinite(e) && e > 0 ? e : 0.5, n = Math.min(JS, Math.max(QS, eT / i)), s = n / 2 / Math.tan(t * Math.PI / 360);
  return { targetY: r - (0.5 - tT) * n, distance: s };
}
const rT = 30;
async function nT(r, e, t) {
  const i = await $S(e, () => t.onDegraded?.());
  let n;
  try {
    n = new Rx({ canvas: r, alpha: !0, antialias: !0, premultipliedAlpha: !1 });
  } catch (S) {
    throw Mu(i), S;
  }
  n.setClearColor(0, 0), n.setPixelRatio(1);
  const s = new wf();
  s.add(new dm(15791352, 1.1));
  const a = new yl(16776181, 1.5);
  a.position.set(1, 2, 3), s.add(a);
  const o = new yl(14543093, 1);
  o.position.set(-1, 1, 3), s.add(o), i.scene.traverse((S) => {
    S.isMesh && (S.frustumCulled = !1);
  }), s.add(i.scene);
  const l = new zt(30, 1, 0.01, 100), c = new P(), h = new P(), d = () => {
    i.scene.updateMatrixWorld(!0);
    const S = i.humanoid?.getNormalizedBoneNode("head")?.getWorldPosition(h), { targetY: T, distance: A } = iT({ headY: S?.y ?? 1.4, aspect: l.aspect, fovDeg: l.fov });
    c.set(S?.x ?? 0, T, S?.z ?? 0), l.position.set(c.x, c.y, c.z + A), l.lookAt(c);
  };
  d();
  const u = DM(i);
  let p = !1, f = 0, v = performance.now(), m = Number.NEGATIVE_INFINITY, g = 0, E = 0, R = !0;
  const y = (S) => {
    if (p) return;
    if (f = requestAnimationFrame(y), document.hidden) {
      v = S;
      return;
    }
    const T = r.clientWidth, A = r.clientHeight;
    if (T === 0 || A === 0) {
      v = S;
      return;
    }
    if (!FM(S, m, rT)) return;
    m = S, (T !== g || A !== E) && (g = T, E = A, n.setSize(g, E, !1), l.aspect = g / E, l.updateProjectionMatrix(), d());
    const x = (S - v) / 1e3;
    v = S;
    const b = t.mouth.speaking;
    u(x, t.mouth.value(t.now()), b ? "speaking" : t.getState(), void 0, b), R && (R = !1, d()), n.render(s, l);
  };
  return f = requestAnimationFrame(y), { dispose() {
    p = !0, cancelAnimationFrame(f), Mu(i), n.dispose();
  } };
}
function sT() {
  const r = document.getElementById("kira-avatar");
  r instanceof HTMLCanvasElement && Jd({ target: document, canvas: r, mouth: new op(), now: () => performance.now() / 1e3, fetchModel: ep, startStage: nT });
}
sT();
