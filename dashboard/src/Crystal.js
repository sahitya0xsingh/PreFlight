// A faceted crystal made of one shard per compared job run, coloured by outcome.
// Plain three.js (no React renderer): one InstancedMesh, a few lights, fog.
import * as THREE from 'three'

const COLORS = {
  agree: new THREE.Color('#cfe9ff'),
  disagree: new THREE.Color('#ff5a4e'),
  excluded: new THREE.Color('#ffc46b'),
  mine: new THREE.Color('#7df9ff'),
}

export function mountCrystal(canvas, shards, { reduced = false } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  renderer.outputColorSpace = THREE.SRGBColorSpace
  renderer.toneMapping = THREE.ACESFilmicToneMapping
  renderer.toneMappingExposure = 1.35

  const scene = new THREE.Scene()
  scene.fog = new THREE.FogExp2(0x05070b, 0.028)
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100)
  camera.position.set(0, 0, 11)

  scene.add(new THREE.HemisphereLight(0xbfe3ff, 0x0a0d14, 0.55))
  const key = new THREE.DirectionalLight(0xffffff, 2.4)
  key.position.set(4, 6, 8)
  scene.add(key)
  const rim = new THREE.PointLight(0x5fd7ff, 40, 30)
  rim.position.set(-6, -2, -4)
  scene.add(rim)
  const warm = new THREE.PointLight(0xffb36b, 14, 20)
  warm.position.set(5, -4, 3)
  scene.add(warm)

  const n = Math.max(1, shards.length)
  const geo = new THREE.OctahedronGeometry(1, 0)
  geo.scale(0.16, 0.42, 0.16)
  const mat = new THREE.MeshPhysicalMaterial({
    metalness: 0.05, roughness: 0.18, clearcoat: 1, clearcoatRoughness: 0.06,
    flatShading: true, transparent: true, opacity: 0.95,
    emissive: new THREE.Color('#0b2236'), emissiveIntensity: 0.6,
  })
  const mesh = new THREE.InstancedMesh(geo, mat, n)
  const group = new THREE.Group()
  group.add(mesh)
  scene.add(group)

  // Shards on a Fibonacci sphere, pointing outward, so the cluster reads as one crystal.
  const base = []
  const golden = Math.PI * (3 - Math.sqrt(5))
  const dummy = new THREE.Object3D()
  const up = new THREE.Vector3(0, 1, 0)
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / Math.max(1, n - 1)) * 2
    const r = Math.sqrt(1 - y * y)
    const t = golden * i
    const dir = new THREE.Vector3(Math.cos(t) * r, y, Math.sin(t) * r)
    const jitter = 0.85 + ((i * 9301 + 49297) % 233280) / 233280 * 0.45
    base.push({ dir, radius: 2.6 * jitter, spin: (i % 7) * 0.9 })
    mesh.setColorAt(i, COLORS[shards[i]] || COLORS.agree)
  }
  mesh.instanceColor.needsUpdate = true

  // A faint wireframe core for depth.
  const core = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1.55, 1),
    new THREE.MeshBasicMaterial({ color: 0x7fd8ff, wireframe: true, transparent: true, opacity: 0.08 }),
  )
  group.add(core)

  // Dust.
  const dustGeo = new THREE.BufferGeometry()
  const dust = new Float32Array(900 * 3)
  for (let i = 0; i < dust.length; i++) dust[i] = (Math.random() - 0.5) * 30
  dustGeo.setAttribute('position', new THREE.BufferAttribute(dust, 3))
  const dustPts = new THREE.Points(dustGeo, new THREE.PointsMaterial({ color: 0x9fd6ff, size: 0.025, transparent: true, opacity: 0.55 }))
  scene.add(dustPts)

  let mx = 0, my = 0, scroll = 0, raf = 0, w = 0, h = 0
  const onMove = (e) => { mx = e.clientX / window.innerWidth - 0.5; my = e.clientY / window.innerHeight - 0.5 }
  const onScroll = () => { scroll = Math.min(1, window.scrollY / (window.innerHeight * 1.2)) }
  window.addEventListener('pointermove', onMove, { passive: true })
  window.addEventListener('scroll', onScroll, { passive: true })

  function resize() {
    w = canvas.clientWidth; h = canvas.clientHeight
    renderer.setSize(w, h, false)
    camera.aspect = w / h
    // Keep the whole crystal in frame on tall, narrow screens.
    camera.position.z = 13 / Math.min(1, camera.aspect * 1.25) + (w < 900 ? 4 : 0)
    camera.updateProjectionMatrix()
  }
  const ro = new ResizeObserver(resize)
  ro.observe(canvas)
  resize()

  const clock = new THREE.Clock()
  function frame() {
    const t = reduced ? 0 : clock.getElapsedTime()
    const burst = 1 + scroll * 0.9
    for (let i = 0; i < n; i++) {
      const b = base[i]
      const breathe = 1 + Math.sin(t * 0.8 + i * 0.37) * 0.03
      dummy.position.copy(b.dir).multiplyScalar(b.radius * breathe * burst)
      dummy.quaternion.setFromUnitVectors(up, b.dir)
      dummy.rotateY(b.spin + t * 0.25)
      dummy.updateMatrix()
      mesh.setMatrixAt(i, dummy.matrix)
    }
    mesh.instanceMatrix.needsUpdate = true
    group.rotation.y = t * 0.12 + mx * 0.6
    group.rotation.x = -0.25 + my * 0.4 + scroll * 0.6
    group.position.x = (w > 900 ? 2.6 : 0) * (1 - scroll * 0.5)
    group.position.y = w > 900 ? 0 : 3.2 + scroll * 2
    core.rotation.y = -t * 0.2
    dustPts.rotation.y = t * 0.01
    // Past the hero, the crystal recedes so the content stays readable.
    canvas.style.opacity = String(1 - scroll * 0.72)
    renderer.render(scene, camera)
    if (!reduced) raf = requestAnimationFrame(frame)
  }
  frame()

  return () => {
    cancelAnimationFrame(raf)
    ro.disconnect()
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('scroll', onScroll)
    geo.dispose(); mat.dispose(); dustGeo.dispose(); renderer.dispose()
  }
}
