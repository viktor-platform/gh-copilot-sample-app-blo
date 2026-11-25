import {
  ArrowHelper, BoxGeometry, BufferAttribute, BufferGeometry, Color, DirectionalLight, Float32BufferAttribute,
  GridHelper, Group, HemisphereLight, LineBasicMaterial, LineSegments, Matrix4, Mesh, MeshBasicMaterial,
  MeshStandardMaterial, OrthographicCamera, Scene, Vector3, WebGLRenderer,
} from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const byId = (id) => document.getElementById(id);
const palette = ["#1a49c5", "#10bfda", "#3ac452", "#f6d348", "#d62626"];
const stops = palette.map((value) => new Color(value));
const colors = {
  column: "#94b5c5", chord: "#68c7ca", purlin: "#68c7ca", web: "#b7cdd3",
  roof_brace: "#f1a361", side_brace: "#f1a361",
};
const format = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
const direction = {
  "3d": new Vector3(1.25, 0.88, -1.4),
  gable: new Vector3(0, 0, 1),
  roof: new Vector3(0, 1, 0.0001),
};

// ETABS X, Y, Z map to right, away, up.
const worldPoint = ([x, y, z]) => new Vector3(x, z, -y);

function jet(value) {
  const index = Math.max(0, Math.min(1, value)) * (stops.length - 1);
  const low = Math.min(Math.floor(index), stops.length - 2);
  return stops[low].clone().lerp(stops[low + 1], index - low);
}

function profileBox(section) {
  const geometry = new BoxGeometry(section.width, section.depth, 1);
  geometry.translate(0, 0, 0.5);
  return geometry;
}

function placeMember(mesh, start, end) {
  const axis = end.clone().sub(start);
  const length = axis.length();
  if (length < 1e-9) {
    mesh.visible = false;
    return;
  }
  axis.divideScalar(length);
  const depth = new Vector3(0, 1, 0);
  if (Math.abs(axis.y) > 0.99) depth.set(1, 0, 0);
  const width = depth.clone().cross(axis).normalize();
  depth.copy(axis).cross(width).normalize();
  mesh.quaternion.setFromRotationMatrix(new Matrix4().makeBasis(width, depth, axis));
  mesh.scale.z = length;
  mesh.position.copy(start);
}

function createScene(container) {
  const scene = new Scene();
  scene.background = new Color("#14282e");
  const renderer = new WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.domElement.setAttribute("role", "img");
  renderer.domElement.setAttribute("aria-label", "Orbitable 3D steel shed with loads and deformation");
  container.append(renderer.domElement);

  const camera = new OrthographicCamera(-1, 1, 1, -1, 0.1, 500);
  const orbit = new OrbitControls(camera, renderer.domElement);
  orbit.minZoom = 0.4;
  orbit.maxZoom = 8;
  orbit.addEventListener("change", () => renderer.render(scene, camera));
  scene.add(new HemisphereLight("#ebfdff", "#51646a", 2.2));
  const sun = new DirectionalLight("#fff0d6", 1.6);
  sun.position.set(10, 25, 12);
  scene.add(sun);

  const grid = new GridHelper(1, 24, "#517078", "#2c4c54");
  const frames = new Group();
  const arrows = new Group();
  const supports = new Group();
  const ghost = new Group();
  scene.add(grid, frames, arrows, supports, ghost);
  const staticMaterials = Object.fromEntries(Object.entries(colors).map(([kind, color]) => [
    kind, new MeshStandardMaterial({ color, roughness: 0.72 }),
  ]));
  const resultMaterial = new MeshBasicMaterial({ vertexColors: true });
  const baseMaterial = new MeshStandardMaterial({ color: "#77d19a" });
  const baseGeometry = new BoxGeometry(0.55, 0.1, 0.55);
  const ghostMaterial = new LineBasicMaterial({ color: "#9fb8b9", transparent: true, opacity: 0.33 });
  const profiles = new Map();
  let center = new Vector3();
  let extent = 10;

  function render() {
    renderer.render(scene, camera);
  }

  function resize() {
    const { clientWidth, clientHeight } = container;
    if (!clientWidth || !clientHeight) return;
    const aspect = clientWidth / clientHeight;
    const height = extent / Math.min(1, aspect);
    camera.left = -height * aspect;
    camera.right = height * aspect;
    camera.top = height;
    camera.bottom = -height;
    camera.updateProjectionMatrix();
    renderer.setSize(clientWidth, clientHeight);
    render();
  }

  function resetView(view) {
    camera.zoom = 1;
    orbit.target.copy(center);
    camera.position.copy(center).addScaledVector(direction[view], 2.8 * extent);
    orbit.update();
    resize();
  }

  function clear(group) {
    for (const object of [...group.children]) {
      if (object.userData.ownedGeometry) object.geometry.dispose();
      if (object instanceof ArrowHelper) object.dispose();
      group.remove(object);
    }
  }

  function loadArrows(data, coordinates, kind) {
    if (kind === "none") return;
    const vertical = data.roof.map((load) => kind === "roof" ? load.dead + load.live : load.uplift);
    const max = Math.max(...vertical.map(Math.abs));
    vertical.forEach((force, i) => {
      if (force === 0) return;
      const length = extent * (0.045 + 0.095 * Math.abs(force) / max);
      const vector = new Vector3(0, Math.sign(force), 0);
      const anchor = coordinates[data.roof[i].joint].clone();
      anchor.addScaledVector(vector, force < 0 ? -length : 0.08);
      arrows.add(new ArrowHelper(vector, anchor, length, kind === "roof" ? "#ffd177" : "#e896df",
        length * 0.32, length * 0.19));
    });
    if (kind === "wind") {
      const peak = Math.max(0, ...data.wind.map(({ line }) => Math.abs(line)));
      for (const { member, line } of data.wind) {
        const beam = data.members[member];
        const vector = new Vector3(Math.sign(line), 0, 0);
        const length = extent * (0.07 + 0.11 * Math.abs(line) / peak);
        for (const level of [0.28, 0.56, 0.84]) {
          const anchor = coordinates[beam.start].clone().lerp(coordinates[beam.end], level);
          anchor.addScaledVector(vector, -length);
          arrows.add(new ArrowHelper(vector, anchor, length, "#e896df", length * 0.32, length * 0.2));
        }
      }
    }
  }

  function draw(data, combo, shape, load, fitView) {
    const originals = data.points.map(worldPoint);
    const height = data.inputs.eave_height + data.inputs.rise;
    center = new Vector3(data.inputs.span / 2, height * 0.4, -data.length / 2);
    extent = 0.47 * Math.hypot(data.inputs.span, data.length, height);
    grid.position.set(center.x, -0.065, center.z);
    grid.scale.setScalar(Math.max(data.inputs.span, data.length) * 1.25);

    for (const group of [frames, arrows, ghost, supports]) clear(group);
    const result = data.combinations?.[combo];
    const useDeformation = Boolean(result && shape !== "original");
    const peak = result ? result.max_displacement_mm : 0;
    const factor = shape === "auto" && peak > 0
      ? Math.min(500, Math.max(1, Math.round(extent * 140 / peak)))
      : 1;
    const vectors = useDeformation ? result.translations_m.map(worldPoint) : [];
    const positions = useDeformation
      ? originals.map((position, i) => position.clone().addScaledVector(vectors[i], factor))
      : originals;
    const vertexColors = useDeformation ? vectors.map((vector) => jet(peak ? vector.length() * 1000 / peak : 0)) : [];
    const originalLines = [];

    for (const { start, end, kind } of data.members) {
      if (!profiles.has(kind)) profiles.set(kind, profileBox(data.sections[kind]));
      const geometry = useDeformation ? profiles.get(kind).clone() : profiles.get(kind);
      if (useDeformation) {
        const colorArray = [];
        const vertices = geometry.getAttribute("position");
        for (let i = 0; i < vertices.count; i++) {
          const color = vertices.getZ(i) > 0.5 ? vertexColors[end] : vertexColors[start];
          colorArray.push(color.r, color.g, color.b);
        }
        geometry.setAttribute("color", new Float32BufferAttribute(colorArray, 3));
        originalLines.push(...originals[start].toArray(), ...originals[end].toArray());
      }
      const mesh = new Mesh(geometry, useDeformation ? resultMaterial : staticMaterials[kind]);
      mesh.userData.ownedGeometry = useDeformation;
      placeMember(mesh, positions[start], positions[end]);
      frames.add(mesh);
    }
    if (useDeformation) {
      const geometry = new BufferGeometry();
      geometry.setAttribute("position", new BufferAttribute(new Float32Array(originalLines), 3));
      const lines = new LineSegments(geometry, ghostMaterial);
      lines.userData.ownedGeometry = true;
      ghost.add(lines);
    }
    for (const index of data.bases) {
      const base = new Mesh(baseGeometry, baseMaterial);
      base.position.copy(originals[index]);
      base.position.y -= 0.04;
      supports.add(base);
    }

    loadArrows(data, originals, load);
    if (fitView) resetView(fitView);
    else render();
    return { useDeformation, factor, peak };
  }

  new ResizeObserver(resize).observe(container);
  return { draw, resetView };
}

let data = JSON.parse(byId("model-data").textContent);
let rendered;
try {
  rendered = createScene(byId("viewport"));
  byId("scene-message").hidden = true;
} catch (error) {
  byId("scene-message").textContent = "The browser could not open the 3D view. Check that WebGL is enabled.";
  console.error(error);
}
const comboSelect = byId("combo");
const loadSelect = byId("loads");
const shapeSelect = byId("shape");

let activeView = "3d";

function update(fit = false) {
  const number = data.inputs.bays;
  byId("dimensions").textContent = `${format.format(data.inputs.span)} m span · ${format.format(data.length)} m long · ${number} ${number === 1 ? "bay" : "bays"}`;
  const totals = data.load_totals;
  byId("load-detail").textContent = loadSelect.value === "none" ? "" : loadSelect.value === "roof"
    ? `Roof dead + live ↓ ${format.format(totals.roof_dead_kn + totals.roof_live_kn)} kN, plus steel self-weight`
    : `Wall ${data.inputs.wind_side} ${format.format(Math.abs(totals.wall_wind_kn))} kN · roof uplift ↑ ${format.format(totals.roof_uplift_kn)} kN`;
  byId("result-controls").hidden = !data.combinations;
  const state = rendered?.draw(data, comboSelect.value, shapeSelect.value, loadSelect.value, fit ? activeView : null);
  byId("heatmap").hidden = !state?.useDeformation;
  byId("part-legend").hidden = Boolean(state?.useDeformation);
  if (state?.useDeformation) {
    byId("max-displacement").textContent = `${format.format(state.peak)} mm`;
    byId("scale-note").textContent = `Shape scale ×${state.factor}`;
    document.querySelector(".ramp").style.background = state.peak > 0
      ? `linear-gradient(90deg, ${palette.join(",")})`
      : palette[0];
  }
}

function applyModel(next, reset = false) {
  data = next;
  comboSelect.replaceChildren();
  for (const [value, label] of Object.entries(data.combo_labels)) {
    comboSelect.append(new Option(label, value));
  }
  comboSelect.value = data.roof_combo;
  loadSelect.value = "roof";
  shapeSelect.value = "auto";
  update(reset);
}

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-view]").forEach((preset) => preset.setAttribute("aria-pressed", String(preset === button)));
    activeView = button.dataset.view;
    rendered?.resetView(activeView);
  });
});
comboSelect.addEventListener("change", () => {
  loadSelect.value = comboSelect.value === data.roof_combo ? "roof" : "wind";
  update();
});
loadSelect.addEventListener("change", () => update());
shapeSelect.addEventListener("change", () => update());
applyModel(data, true);

if (data.fields) {
  byId("local-inputs").hidden = false;
  const form = byId("model-form");
  const fields = byId("fields");
  const message = byId("input-message");
  const modelLink = byId("model-link");
  const runButton = byId("run-etabs");
  const note = data.solver_note;
  runButton.disabled = Boolean(note);
  byId("solver-note").textContent = note ?? "ETABS creates a new model for each run.";
  for (const spec of data.fields) {
    const field = document.createElement("input");
    field.type = "number";
    field.required = true;
    field.name = spec.key;
    field.min = spec.minimum;
    field.max = spec.maximum;
    field.step = spec.step;
    field.value = data.inputs[spec.key];
    const label = document.createElement("label");
    label.append(spec.unit ? `${spec.label} (${spec.unit})` : spec.label, field);
    fields.append(label);
  }
  const side = document.createElement("select");
  side.name = "wind_side";
  side.append(new Option("+X", "+X"), new Option("−X", "-X"));
  side.value = data.inputs.wind_side;
  const label = document.createElement("label");
  label.append("Wind direction", side);
  fields.append(label);

  let previewTimer;
  let previewRequest;
  let running = false;

  function readInputs() {
    const values = {};
    fields.querySelectorAll("input").forEach((input) => { values[input.name] = input.valueAsNumber; });
    values.wind_side = side.value;
    return values;
  }

  async function requestModel(path, values, signal) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
      signal,
    });
    const next = await response.json();
    if (!response.ok) throw new Error(next.error);
    return next;
  }

  async function refreshPreview() {
    const request = new AbortController();
    previewRequest = request;
    try {
      const next = await requestModel("/api/preview", readInputs(), request.signal);
      if (request.signal.aborted) return;
      const fit = next.length !== data.length ||
        ["span", "eave_height", "rise"].some((key) => next.inputs[key] !== data.inputs[key]);
      data = next;
      update(fit);
      message.textContent = "";
    } catch (error) {
      if (!request.signal.aborted) message.textContent = error.message;
    }
  }

  form.addEventListener("input", () => {
    clearTimeout(previewTimer);
    previewRequest?.abort();
    modelLink.hidden = true;
    if (data.combinations) {
      data.combinations = null;
      update();
    }
    const valid = form.checkValidity();
    runButton.disabled = Boolean(note) || !valid;
    message.textContent = valid ? "Updating the preview…" : "Enter values within the input ranges.";
    if (valid) previewTimer = setTimeout(refreshPreview, 160);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (note || running || !form.reportValidity()) return;
    clearTimeout(previewTimer);
    previewRequest?.abort();
    const values = readInputs();
    running = true;
    for (const element of form.elements) element.disabled = true;
    modelLink.hidden = true;
    if (data.combinations) {
      data.combinations = null;
      update();
    }
    message.textContent = "Creating the shed and running ETABS…";
    try {
      const next = await requestModel("/api/run", values);
      applyModel(next, true);
      modelLink.href = next.model_url;
      modelLink.hidden = false;
      message.textContent = `ETABS finished. Roof deformation is shown. ${next.build_method}.`;
    } catch (error) {
      message.textContent = error.message;
    } finally {
      running = false;
      for (const element of form.elements) element.disabled = false;
    }
  });
}
