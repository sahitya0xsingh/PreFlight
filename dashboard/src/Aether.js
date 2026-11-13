// Soft volumetric "aether": slow domain-warped fog in the theme's two accents, rendered at low
// resolution behind everything (the browser's upscale makes it feel like light through mist).
import * as THREE from 'three'

const FRAG = /* glsl */ `
  precision highp float;
  uniform float uTime, uScroll;
  uniform vec2 uRes, uMouse;
  uniform vec3 uA, uB, uBg;
  varying vec2 vUv;
  float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), u.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), u.x), u.y);
  }
  float fbm(vec2 p) {
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 5; i++) { v += a * noise(p); p = p * 2.03 + 11.7; a *= 0.5; }
    return v;
  }
  void main() {
    vec2 uv = vUv;
    vec2 p = (uv - 0.5) * vec2(uRes.x / uRes.y, 1.0) * 2.2;
    float t = uTime * 0.035;
    p += (uMouse - 0.5) * 0.25;
    p.y += uScroll * 1.6;
    // domain warping: fog folding over itself
    vec2 q = vec2(fbm(p + t), fbm(p + vec2(5.2, 1.3) - t));
    vec2 r = vec2(fbm(p + 3.0 * q + vec2(1.7, 9.2) + t * 1.3), fbm(p + 3.0 * q + vec2(8.3, 2.8) - t));
    float f = fbm(p + 2.6 * r);
    vec3 col = mix(uBg, uA, smoothstep(0.35, 0.95, f) * 0.32);
    col = mix(col, uB, smoothstep(0.45, 1.0, length(r)) * 0.22);
    // light shafts from the top right
    float shaft = pow(max(0.0, 1.0 - length(uv - vec2(0.82, 1.05)) * 0.95), 3.0);
    col += uA * shaft * 0.14;
    // vignette back into the page colour
    float v = smoothstep(1.25, 0.25, length((uv - 0.5) * vec2(1.25, 1.0)));
    col = mix(uBg, col, v);
    gl_FragColor = vec4(col, 1.0);
  }
`

function cssRgb(name) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  const [r, g, b] = v.split(',').map((x) => parseFloat(x) / 255)
  return new THREE.Color(r || 0, g || 0, b || 0)
}

export function mountAether(canvas, { reduced = false } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: false, powerPreference: 'low-power' })
  renderer.setPixelRatio(0.35)   // deliberately soft
  const scene = new THREE.Scene()
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1)
  const uniforms = {
    uTime: { value: 0 }, uScroll: { value: 0 }, uRes: { value: new THREE.Vector2(1, 1) },
    uMouse: { value: new THREE.Vector2(0.5, 0.5) },
    uA: { value: new THREE.Color() }, uB: { value: new THREE.Color() }, uBg: { value: new THREE.Color() },
  }
  const mat = new THREE.ShaderMaterial({
    uniforms, fragmentShader: FRAG,
    vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }',
    depthTest: false, depthWrite: false,
  })
  scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2, 2), mat))

  const readTheme = () => {
    uniforms.uA.value.copy(cssRgb('--accent-rgb'))
    uniforms.uB.value.copy(cssRgb('--accent2-rgb'))
    uniforms.uBg.value.copy(cssRgb('--bg-rgb'))
  }
  readTheme()
  const mo = new MutationObserver(readTheme)
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })

  const target = new THREE.Vector2(0.5, 0.5)
  const onMove = (e) => target.set(e.clientX / innerWidth, 1 - e.clientY / innerHeight)
  addEventListener('pointermove', onMove, { passive: true })
  const resize = () => {
    renderer.setSize(canvas.clientWidth, canvas.clientHeight, false)
    uniforms.uRes.value.set(canvas.clientWidth, canvas.clientHeight)
  }
  const ro = new ResizeObserver(resize)
  ro.observe(canvas)
  resize()

  let raf = 0
  const clock = new THREE.Clock()
  const frame = () => {
    uniforms.uTime.value = reduced ? 20 : clock.getElapsedTime() + 20
    uniforms.uScroll.value = scrollY / Math.max(1, document.body.scrollHeight - innerHeight)
    uniforms.uMouse.value.lerp(target, 0.03)
    renderer.render(scene, camera)
    if (!reduced) raf = requestAnimationFrame(frame)
  }
  frame()
  return () => {
    cancelAnimationFrame(raf); ro.disconnect(); mo.disconnect()
    removeEventListener('pointermove', onMove)
    mat.dispose(); renderer.dispose()
  }
}
