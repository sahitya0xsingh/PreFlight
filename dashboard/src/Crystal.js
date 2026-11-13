// A sphere of code: one glowing label per compared job run (its real job name), coloured by
// outcome, inside a cloud of code tokens. The labels assemble on load and explode on scroll.
// Plain three.js: one instanced quad mesh, billboarded and animated in the vertex shader.
import * as THREE from 'three'

// Colours per theme (the page's palette picker); disagree/excluded keep their meaning everywhere.
export const PALETTES = {
  ice: { agree: '#d9f1ff', mine: '#5ee4ff', dust: '#33508c', core: '#5b7cff', disagree: '#ff5a4e', excluded: '#ffc46b' },
  aurora: { agree: '#dcfff3', mine: '#2ef2b8', dust: '#4b3a8a', core: '#9b6bff', disagree: '#ff5a4e', excluded: '#ffc46b' },
  nebula: { agree: '#f1e6ff', mine: '#ff5fb7', dust: '#4a3480', core: '#c38bff', disagree: '#ff4d3d', excluded: '#ffd27a' },
  solar: { agree: '#fff0dc', mine: '#ffb547', dust: '#7a3a3a', core: '#ff5e7e', disagree: '#ff3b3b', excluded: '#ffe08a' },
  paper: { agree: '#1b2638', mine: '#2556e6', dust: '#b4b9c4', core: '#7c3aed', disagree: '#d63a2f', excluded: '#b7791f', light: true },
}

const CODE = [
  '{', '}', '=>', '&&', '||', '</>', '()', '[]', ';', '$', '#!', '::', '!=', '==', '++', '/*', '*/', '//',
  'uses:', 'run:', 'needs:', 'if:', 'with:', 'steps:', 'jobs:', 'on: pull_request', 'runs-on: ubuntu-latest',
  '$ mvn verify', '$ npm test', '$ pytest', '$ tox -e typing', '$ git push', 'git merge', 'exit 0', 'exit 1',
  'matrix.os', 'actions/checkout', 'setup-java', 'setup-node', 'setup-python', 'docker run', 'FROM ubuntu',
  'def main():', 'const ci =', 'import yaml', 'assert ok', 'return 0', 'throw err', 'catch (e)', 'await run()',
  '0x1f', '200 OK', 'PASS', 'FAIL', '✓', '✗', 'SHA 7f3a9c', 'HEAD', 'origin/main', '--docker', '-j 4',
]

// Draw every distinct label once into a texture atlas; each instance points at its cell.
function atlas(labels) {
  const COLS = 8, CW = 256, CH = 64
  const rows = Math.ceil(labels.length / COLS)
  const c = document.createElement('canvas')
  c.width = COLS * CW
  c.height = THREE.MathUtils.ceilPowerOfTwo(Math.max(1, rows) * CH)
  const g = c.getContext('2d')
  g.fillStyle = '#fff'
  g.textBaseline = 'middle'
  g.textAlign = 'center'
  labels.forEach((t, i) => {
    let size = 34
    g.font = `500 ${size}px "JetBrains Mono", ui-monospace, Menlo, monospace`
    while (g.measureText(t).width > CW - 16 && size > 12) {
      size -= 2
      g.font = `500 ${size}px "JetBrains Mono", ui-monospace, Menlo, monospace`
    }
    g.fillText(t, (i % COLS) * CW + CW / 2, Math.floor(i / COLS) * CH + CH / 2)
  })
  const tex = new THREE.CanvasTexture(c)
  tex.anisotropy = 4
  tex.colorSpace = THREE.SRGBColorSpace
  return { tex, cols: COLS, cellW: CW / c.width, cellH: CH / c.height }
}

const VERT = /* glsl */ `
  attribute vec3 aDir;
  attribute float aRadius;
  attribute float aSeed;
  attribute float aScale;
  attribute vec2 aCell;
  attribute vec3 aColor;
  uniform float uTime, uScroll, uIntro, uCellW, uCellH;
  varying vec2 vUv;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    // Assemble from far away on load; burst outward (each label at its own speed) on scroll.
    float intro = 1.0 + (1.0 - uIntro) * (3.0 + aSeed * 5.0);
    float burst = 1.0 + uScroll * (1.2 + aSeed * 3.5);
    vec3 drift = 0.08 * vec3(sin(uTime * 0.7 + aSeed * 40.0), cos(uTime * 0.6 + aSeed * 23.0), sin(uTime * 0.5 + aSeed * 11.0));
    vec3 center = aDir * aRadius * intro * burst + drift;
    vec4 mv = modelViewMatrix * vec4(center, 1.0);
    mv.xy += position.xy * aScale;            // billboard: the label always faces the camera
    gl_Position = projectionMatrix * mv;
    vUv = vec2(aCell.x + uv.x * uCellW, 1.0 - (aCell.y + (1.0 - uv.y) * uCellH));
    vColor = aColor;
    float depth = clamp((-mv.z - 6.0) / 18.0, 0.0, 1.0);
    float flicker = 0.82 + 0.18 * sin(uTime * 2.3 + aSeed * 60.0);
    float near = smoothstep(5.0, 9.0, -mv.z);   // tokens flying past the camera fade out
    vAlpha = mix(1.0, 0.18, depth) * flicker * uIntro * near;
  }
`

const FRAG = /* glsl */ `
  uniform sampler2D uAtlas;
  varying vec2 vUv;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    float a = texture2D(uAtlas, vUv).a * vAlpha;
    if (a < 0.02) discard;
    gl_FragColor = vec4(vColor * 1.25, a);
  }
`

export function mountCrystal(canvas, shards, { reduced = false, palette = 'ice' } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  const scene = new THREE.Scene()
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100)
  camera.position.set(0, 0, 13)

  // Data labels (one per job run) + a dust of code tokens around them.
  const runs = shards.length ? shards : [{ o: 'agree', label: 'ci' }]
  const DUST = 520
  const labels = [...new Set([...runs.map((s) => s.label), ...CODE])]
  const cellOf = new Map(labels.map((t, i) => [t, i]))
  const { tex, cols, cellW, cellH } = atlas(labels)

  const total = runs.length + DUST
  const quad = new THREE.PlaneGeometry(1, 0.25)
  const geo = new THREE.InstancedBufferGeometry()
  geo.index = quad.index
  geo.setAttribute('position', quad.getAttribute('position'))
  geo.setAttribute('uv', quad.getAttribute('uv'))
  geo.instanceCount = total

  const aDir = new Float32Array(total * 3), aRadius = new Float32Array(total), aSeed = new Float32Array(total)
  const aScale = new Float32Array(total), aCell = new Float32Array(total * 2), aColor = new Float32Array(total * 3)
  const golden = Math.PI * (3 - Math.sqrt(5))
  const rand = (i) => ((Math.sin(i * 12.9898) * 43758.5453) % 1 + 1) % 1
  const col = new THREE.Color()
  for (let i = 0; i < total; i++) {
    const isRun = i < runs.length
    let dir
    if (isRun) {   // job runs on a Fibonacci shell: an even sphere of labels
      const n = runs.length, y = 1 - (i / Math.max(1, n - 1)) * 2, r = Math.sqrt(1 - y * y), t = golden * i
      dir = new THREE.Vector3(Math.cos(t) * r, y, Math.sin(t) * r)
    } else {       // code dust: random directions, wider spread
      dir = new THREE.Vector3(rand(i) - 0.5, rand(i + 7) - 0.5, rand(i + 13) - 0.5).normalize()
    }
    aDir.set([dir.x, dir.y, dir.z], i * 3)
    aRadius[i] = isRun ? 2.6 + rand(i + 3) * 0.5 : 3.6 + rand(i + 5) * 7
    aSeed[i] = rand(i + 11)
    aScale[i] = isRun ? 0.78 : 0.4 + rand(i + 17) * 0.35
    const label = isRun ? runs[i].label : CODE[Math.floor(rand(i + 19) * CODE.length)]
    const cell = cellOf.get(label)
    aCell.set([(cell % cols) * cellW, Math.floor(cell / cols) * cellH], i * 2)
  }
  const kinds = Array.from({ length: total }, (_, i) => (i < runs.length ? runs[i].o : 'dust'))
  const attr = (a, n) => new THREE.InstancedBufferAttribute(a, n)
  geo.setAttribute('aDir', attr(aDir, 3))
  geo.setAttribute('aRadius', attr(aRadius, 1))
  geo.setAttribute('aSeed', attr(aSeed, 1))
  geo.setAttribute('aScale', attr(aScale, 1))
  geo.setAttribute('aCell', attr(aCell, 2))
  const colorAttr = attr(aColor, 3)
  geo.setAttribute('aColor', colorAttr)

  const uniforms = {
    uTime: { value: 0 }, uScroll: { value: 0 }, uIntro: { value: reduced ? 1 : 0 },
    uAtlas: { value: tex }, uCellW: { value: cellW }, uCellH: { value: cellH },
  }
  const mat = new THREE.ShaderMaterial({
    uniforms, vertexShader: VERT, fragmentShader: FRAG,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  })
  // Re-colour every label for a palette; light themes draw dark ink with normal blending.
  function setPalette(name) {
    const pal = PALETTES[name] || PALETTES.ice
    kinds.forEach((k, i) => { col.set(pal[k] || pal.agree); aColor.set([col.r, col.g, col.b], i * 3) })
    colorAttr.needsUpdate = true
    mat.blending = pal.light ? THREE.NormalBlending : THREE.AdditiveBlending
    mat.needsUpdate = true
    core.material.color.set(pal.core)
  }
  const mesh = new THREE.Mesh(geo, mat)
  mesh.frustumCulled = false
  const group = new THREE.Group()
  group.add(mesh)

  // A faint wireframe core, so the sphere reads as one object.
  const core = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1.7, 1),
    new THREE.MeshBasicMaterial({ color: 0x7fd8ff, wireframe: true, transparent: true, opacity: 0.07 }),
  )
  group.add(core)
  scene.add(group)
  setPalette(palette)

  let mx = 0, my = 0, scroll = 0, raf = 0, w = 0, h = 0
  const onMove = (e) => { mx = e.clientX / window.innerWidth - 0.5; my = e.clientY / window.innerHeight - 0.5 }
  const onScroll = () => { scroll = Math.min(1, window.scrollY / (window.innerHeight * 1.1)) }
  window.addEventListener('pointermove', onMove, { passive: true })
  window.addEventListener('scroll', onScroll, { passive: true })

  function resize() {
    w = canvas.clientWidth; h = canvas.clientHeight
    renderer.setSize(w, h, false)
    camera.aspect = w / h
    // Keep the whole sphere in frame on tall, narrow screens.
    camera.position.z = 13 / Math.min(1, camera.aspect * 1.25) + (w < 900 ? 4 : 0)
    camera.updateProjectionMatrix()
  }
  const ro = new ResizeObserver(resize)
  ro.observe(canvas)
  resize()

  // Re-draw the atlas once the web font has loaded, so labels use JetBrains Mono.
  document.fonts?.ready.then(() => {
    const fresh = atlas(labels)
    uniforms.uAtlas.value = fresh.tex
    tex.dispose()
  })

  const clock = new THREE.Clock()
  function frame() {
    const t = reduced ? 0 : clock.getElapsedTime()
    uniforms.uTime.value = t
    uniforms.uScroll.value = scroll
    if (!reduced) uniforms.uIntro.value = 1 - Math.pow(1 - Math.min(1, t / 2.6), 4)
    group.rotation.y = t * 0.1 + mx * 0.6
    group.rotation.x = -0.2 + my * 0.4 + scroll * 0.5
    group.position.x = (w > 900 ? 2.8 : 0) * (1 - scroll * 0.6)
    group.position.y = w > 900 ? 0 : 3.2 + scroll * 2
    core.rotation.y = -t * 0.2
    core.material.opacity = 0.07 * (1 - scroll)
    canvas.style.opacity = String(1 - scroll * 0.55)
    renderer.render(scene, camera)
    if (!reduced) raf = requestAnimationFrame(frame)
  }
  frame()

  const dispose = () => {
    cancelAnimationFrame(raf)
    ro.disconnect()
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('scroll', onScroll)
    geo.dispose(); quad.dispose(); mat.dispose(); uniforms.uAtlas.value.dispose(); renderer.dispose()
  }
  return { dispose, setPalette }
}
