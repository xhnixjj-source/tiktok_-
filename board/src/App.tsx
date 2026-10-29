import { createContext, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  useEdges,
  useEdgesState,
  useNodes,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { request } from "./api.ts";

type Scene = { id: string; name: string; description: string; image: string; active: boolean };
type SpecBlock = { label: string; value: string };
type Product = { id: string; title: string; price: string; points: string; profile_text?: string; page_text: string; status: string; detail: string; source_url: string; cover?: string; specs?: SpecBlock[] };
type Job = { id: string; product_id: string; kind: string; node_id: string; label: string; status: string; error: string };
const JOB_STAGE: Record<string, string> = { queued: "排队", running: "生成中", done: "完成", failed: "失败" };

function jobOpen(job: Job) {
  return job.status === "queued" || job.status === "running";
}

function jobMark(jobs: Job[]) {
  const active = jobs.filter(jobOpen);
  if (active.length) return `${active.length} 个任务`;
  if (jobs.some((job) => job.status === "failed")) return "有失败";
  if (jobs.some((job) => job.status === "done")) return "已完成";
  return "";
}
type Person = { id: string; name: string; image: string; images?: string[]; note: string; active?: boolean };
type PromptItem = { id: string; name: string; body: string; body_zh?: string; tag?: string; product_id: string; product_title: string; scene_id?: string; scene_name?: string; origin?: string; created_at?: string };
const PROMPT_TAGS = ["优质", "低质", "需要修改"];
type HitItem = { id: string; name: string; note: string; path: string; kind: string; file: string; product_id: string; product_title: string; shot_prompt?: string };
type Section = "products" | "scenes" | "people" | "prompts" | "hits" | "speech";
type Shot = { ordinal: number; t_start: number; t_end: number; action: string; voiceover: string; camera: string };
type Check = { name: string; ok: boolean; detail: string };
type VariantBundle = { variant: { id: string; status: string; round: number; adopted: number; video?: string; qa_note: string; check: Check[]; prompt_text?: string; prompt_zh?: string; steer?: string; voiceover?: number; template_id?: string; duration_sec?: number }; shots: Shot[] };
type TemplateItem = { id: string; name: string; body: string; builtin: number };
type SpeechItem = { id: string; label: string; status: string; product_id: string; product_title: string; scene_name: string; video: string; speech_text: string; speech_note: string; speech_ok: number | null; tags?: string[] };
const STEER_PRESETS = [
  ["锁产品图", "加强约束：商品外观、颜色和形状只锁产品图。"],
  ["手不要挡", "手不要挡住商品。"],
  ["不写价格", "不要写价格，也不要写页面上没有的尺寸、功率和功效。"],
] as const;
type GraphKind = "scene" | "facts" | "person" | "voice" | "note" | "viral" | "output" | "storyboard";
type StoryShot = { ordinal: number; seconds: number; prompt: string; image?: string; assetId?: string; error?: string };

const STATUS: Record<string, string> = {
  collecting: "正在抓取",
  fetching: "正在打开页面",
  needs_input: "待手填",
  ready: "已就绪",
  draft: "草稿",
  passed: "已通过",
  needs_human: "需人工",
  queued: "已入队",
  running: "生成中",
  returned: "已回片",
  hold: "留到下一晚",
  failed: "生成失败",
};

function statusText(status: string) {
  return STATUS[status] || status;
}

type NextItem = { title: string; hint: string; step?: GraphKind; sceneId?: string; mark?: string };
type CanvasApi = {
  remove: (id: string) => void;
  templates: TemplateItem[];
  setTemplate: (id: string, templateId: string) => void;
  openImages: (images: string[], index: number, promote?: boolean) => void;
  uploadWhites: (files: File[]) => Promise<void>;
  removeWhite: (assetId: string) => void;
  openUsageSheet: () => void;
  usagePreparing: boolean;
  removeUsage: (assetId: string) => void;
  openWhiteSheet: () => void;
  openDetailSheet: () => void;
  saveStory: (id: string, patch: Record<string, unknown>) => void;
  rememberStory: (id: string, patch: { duration?: number; shotCount?: number }) => void;
  readStory: (id: string) => { duration: number; shotCount: number };
  jobs: Job[];
  refreshJobs: () => Promise<void>;
};
const CanvasActions = createContext<CanvasApi | null>(null);
const NEXT_ITEMS: NextItem[] = [
  { title: "场景图", hint: "再接一张场景", step: "scene" },
  { title: "产品信息约束", hint: "页面上的标题、价格、卖点", step: "facts" },
  { title: "模特", hint: "可以接好几个", step: "person" },
  { title: "口播", hint: "这段话自己写", step: "voice" },
  { title: "自定义", hint: "想补充的任何内容", step: "note" },
  { title: "爆款复刻", hint: "只用分镜和运镜", step: "viral" },
  { title: "成片", hint: "把接到前面的材料收成一条", step: "output" },
  { title: "分镜头生成", hint: "商品、爆款、模特、场景各接一口", step: "storyboard", mark: "镜" },
];

function RemoveButton({ id }: { id: string }) {
  const actions = useContext(CanvasActions);
  return <button type="button" className="node-x nodrag" aria-label="删除节点" onClick={(event) => { event.stopPropagation(); actions?.remove(id); }}>×</button>;
}

function nodeImages(image: string | undefined, images: string[] | undefined) {
  const shots = (images?.length ? images : image ? [image] : []).filter(Boolean);
  return shots;
}

function NodeFilm({ images, promote }: { images: string[]; promote?: boolean }) {
  const actions = useContext(CanvasActions);
  const scroller = useRef<HTMLDivElement>(null);
  const [index, setIndex] = useState(0);
  const shots = images.filter(Boolean);
  if (!shots.length) return null;

  function remember() {
    const element = scroller.current;
    if (!element || element.clientHeight === 0) return;
    const next = Math.round(element.scrollTop / element.clientHeight);
    setIndex(Math.min(shots.length - 1, Math.max(0, next)));
  }

  return (
    <>
      <div className="film nowheel nodrag nopan" ref={scroller} onScroll={remember}>
        {shots.map((src, imageIndex) => (
          <button
            key={`${src}-${imageIndex}`}
            type="button"
            className="film-shot nodrag nopan"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => { event.stopPropagation(); actions?.openImages(shots, imageIndex, promote); }}
          >
            <img src={src} alt={`图 ${imageIndex + 1}`} />
          </button>
        ))}
      </div>
      <span className="film-index">{`${index + 1} / ${shots.length}`} · 点图放大</span>
    </>
  );
}

function SceneNode({ id, data }: NodeProps<Node<{ label: string; description: string; image: string; images?: string[]; sceneId: string }>>) {
  return (
    <div className="node wide">
      <div className="node-head"><span className="node-mark">景</span><strong>{data.label}</strong></div>
      <RemoveButton id={id} />
      <NodeFilm images={nodeImages(data.image, data.images)} />
      <p className="desc">{data.description}</p>
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function ProductNode({ data }: NodeProps<Node<{ label: string; count: number; images: string[]; whites?: string[]; whiteIds?: string[]; details?: string[]; detailIds?: string[]; usages?: string[]; usageIds?: string[]; profile?: string }>>) {
  const actions = useContext(CanvasActions);
  const whites = data.whites || [];
  const details = data.details || [];
  const usages = data.usages || [];
  const hasProfile = Boolean((data.profile || "").trim());
  const detailTitle = !hasProfile ? "先补上产品信息" : whites.length ? "" : "先生成白底图";
  const usageTitle = !hasProfile ? "先补上产品信息" : details.length ? "" : "先生成细节图";
  const openJob = (kind: string) => (actions?.jobs || []).find((job) => job.kind === kind && jobOpen(job));
  const whiteJob = openJob("white");
  const detailJob = openJob("detail");
  const usageJob = openJob("usage");
  return (
    <div className="node gallery">
      <div className="node-head"><span className="node-mark">商</span><strong>{data.label}</strong></div>
      <span className="kind">一个商品 · {data.count} 张图</span>
      {data.images?.length ? <NodeFilm images={data.images} promote /> : <span className="film-index">没有图</span>}
      <div className="white-box nodrag nopan" onPointerDown={(event) => event.stopPropagation()} onMouseDown={(event) => event.stopPropagation()}>
        <div className={details.length ? "asset-row" : undefined}>
          <div className="asset-col">
            <span className="kind">{whites.length ? `白底图 · ${whites.length} 张` : "白底图"}</span>
            {whites.length ? (
              <div className="card-whites">
                {whites.map((src, index) => (
                  <figure key={`${src}-${index}`}>
                    <img src={src} alt={`白底图 ${index + 1}`} onClick={() => actions?.openImages(whites, index)} />
                  </figure>
                ))}
              </div>
            ) : <span className="film-index">还没有白底图</span>}
          </div>
          {details.length ? (
            <div className="asset-col">
              <span className="kind">细节图 · {details.length} 张</span>
              <div className="card-whites">
                {details.map((src, index) => (
                  <figure key={`${src}-${index}`}>
                    <img src={src} alt={`细节图 ${index + 1}`} onClick={() => actions?.openImages(details, index)} />
                  </figure>
                ))}
              </div>
            </div>
          ) : null}
        </div>
        <div className="white-actions">
          <button type="button" className="white-upload" disabled={!hasProfile || Boolean(whiteJob)} title={whiteJob ? "这一项已经提交" : hasProfile ? (whites.length ? "重新生成白底图" : "") : "先补上产品信息"} onClick={() => actions?.openWhiteSheet()}>{whiteJob ? `${whiteJob.label}${whiteJob.status === "queued" ? "排队" : "生成中"}` : whites.length ? "重新生成" : "生成白底图"}</button>
          <button type="button" className="white-upload" disabled={!hasProfile || !whites.length || Boolean(detailJob)} title={detailJob ? "这一项已经提交" : detailTitle || (details.length ? "重新生成细节图" : "")} onClick={() => actions?.openDetailSheet()}>{detailJob ? `${detailJob.label}${detailJob.status === "queued" ? "排队" : "生成中"}` : details.length ? "重新生成" : "生成细节图"}</button>
        </div>
      </div>
      <div className="usage-box nodrag nopan" onPointerDown={(event) => event.stopPropagation()} onMouseDown={(event) => event.stopPropagation()}>
        <span className="kind">{usages.length ? `使用场景图 · ${usages.length} 张` : "使用场景图"}</span>
        {usages.length ? (
          <div className="card-whites">
            {usages.map((src, index) => (
              <figure key={`${src}-${index}`}>
                <img src={src} alt={`使用场景图 ${index + 1}`} onClick={() => actions?.openImages(usages, index)} />
              </figure>
            ))}
          </div>
        ) : <span className="film-index">还没有使用场景图</span>}
        <button type="button" className="white-upload" disabled={!hasProfile || !details.length || Boolean(actions?.usagePreparing) || Boolean(usageJob)} title={usageJob ? "这一项已经提交" : usageTitle} onClick={() => actions?.openUsageSheet()}>{usageJob ? `${usageJob.label}${usageJob.status === "queued" ? "排队" : "生成中"}` : actions?.usagePreparing ? "正在整理提示词" : "生成使用场景图"}</button>
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function FactsNode({ id, data }: NodeProps<Node<{ title: string; price: string; points: string; standard?: boolean }>>) {
  return (
    <div className="node wide">
      <div className="node-head"><span className="node-mark">束</span><strong>产品信息</strong>{data.standard ? <span className="node-tag">AI生成</span> : null}</div>
      <RemoveButton id={id} />
      <strong>{data.title}</strong>
      <p className="desc">{data.price}</p>
      <p className="desc">{data.points}</p>
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function TextNode({ id, data }: NodeProps<Node<{ label: string; text: string }>>) {
  return (
    <div className="node wide">
      <div className="node-head"><span className="node-mark">文</span><strong>{data.label}</strong></div>
      <RemoveButton id={id} />
      <p className="desc">{data.text || "点开后自己写"}</p>
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function AssetNode({ id, data }: NodeProps<Node<{ kind: string; kindLabel: string; label: string; image: string; images?: string[]; detail: string }>>) {
  return (
    <div className="node">
      <div className="node-head"><span className="node-mark">素</span><strong>{data.label}</strong></div>
      {data.kind === "person" || data.kind === "reference" ? <RemoveButton id={id} /> : null}
      <span className="kind">{data.kindLabel}</span>
      <NodeFilm images={nodeImages(data.image, data.images)} />
      {data.detail ? <p className="desc">{data.detail}</p> : null}
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function SlotNode({ id, data }: NodeProps<Node<{ title: string; hint: string }>>) {
  return (
    <div className="node slot">
      <div className="node-head"><span className="node-mark">待</span><strong>{data.title}</strong></div>
      <RemoveButton id={id} />
      <p className="desc">{data.hint}</p>
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function VariantNode({ id, data }: NodeProps<Node<{ label: string; status: string; adopted: boolean; templateId?: string }>>) {
  const actions = useContext(CanvasActions);
  const filming = data.status === "queued" || data.status === "running";
  return (
    <div className={["node", data.adopted ? "adopted" : "", filming ? "filming" : ""].filter(Boolean).join(" ")}>
      <div className="node-head"><span className="node-mark">片</span><strong>{data.label}</strong></div>
      {data.status === "running" || data.status === "queued" ? null : <RemoveButton id={id} />}
      <span className="kind">{filming ? "正在生成视频" : data.adopted ? "已采用" : statusText(data.status)}</span>
      <label className="node-template nodrag nopan">模板
        <select
          value={data.templateId || ""}
          onPointerDown={(event) => event.stopPropagation()}
          onChange={(event) => actions?.setTemplate(id, event.target.value)}
        >
          {(actions?.templates || []).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </label>
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const STORY_PORTS = [
  { id: "product", label: "商品", top: 128, color: "#3d7eff" },
  { id: "viral", label: "爆款", top: 154, color: "#d08a2f" },
  { id: "person", label: "模特", top: 180, color: "#c45c8a" },
  { id: "scene", label: "场景", top: 206, color: "#2f9e6b" },
] as const;
const STORY_COLORS: Record<string, string> = { product: "#3d7eff", viral: "#d08a2f", person: "#c45c8a", scene: "#2f9e6b" };
const VIRAL_EMPTY = new Set(["", "选一条已整理好的分镜", "这条视频的分镜还没整理好"]);

function storyConnectionOk(nodes: Node[], edges: Edge[], connection: { source?: string | null; target?: string | null; targetHandle?: string | null }) {
  const source = nodes.find((node) => node.id === connection.source);
  const target = nodes.find((node) => node.id === connection.target);
  if (!source || !target) return false;
  if (source.type !== "storyboard" && target.type !== "storyboard") return true;
  if (target.type !== "storyboard" || source.type === "storyboard") return false;
  const port = storyPort(source);
  if (!port || connection.targetHandle !== port) return false;
  return !edges.some((edge) => edge.target === target.id && edge.targetHandle === port);
}

function storyPort(node: { type?: string; data?: { kind?: string } } | undefined) {
  if (!node) return "";
  if (node.type === "product") return "product";
  if (node.type === "viral") return "viral";
  if (node.type === "scene") return "scene";
  if (node.type === "asset" && node.data?.kind === "person") return "person";
  return "";
}

function storyCandidates(nodes: Node[], edges: Edge[], nodeId: string) {
  const items: { id: string; src: string; badge: string }[] = [];
  for (const edge of edges) {
    if (edge.target !== nodeId) continue;
    const source = nodes.find((node) => node.id === edge.source);
    const port = edge.targetHandle || storyPort(source);
    const data = (source?.data || {}) as Record<string, unknown>;
    if (port === "product") {
      const pairs: [string, unknown, unknown][] = [
        ["白底图", data.whiteIds, data.whites],
        ["细节图", data.detailIds, data.details],
        ["使用场景图", data.usageIds, data.usages],
      ];
      for (const [badge, ids, srcs] of pairs) {
        const keys = Array.isArray(ids) ? ids.map(String) : [];
        const images = Array.isArray(srcs) ? srcs.map(String) : [];
        keys.forEach((id, index) => {
          if (id && images[index]) items.push({ id, src: images[index], badge });
        });
      }
    }
    if (port === "person") {
      const images = Array.isArray(data.images) ? data.images.map(String) : [];
      const src = images[0] || String(data.image || "");
      if (source && src) items.push({ id: source.id, src, badge: "模特" });
    }
    if (port === "scene") {
      const sceneId = String(data.sceneId || "");
      const src = String(data.image || "");
      if (sceneId && src) items.push({ id: `scene:${sceneId}`, src, badge: "场景" });
    }
  }
  return items;
}

function storyState(id: string, data: { shots?: StoryShot[]; image?: string; clip?: string; clipStatus?: string }, jobs: Job[]) {
  const open = jobs.filter((job) => job.node_id === id && jobOpen(job));
  const video = open.find((job) => job.kind === "storyboard_video");
  const image = open.find((job) => job.kind === "storyboard");
  if (video) return video.status === "queued" ? "分镜视频排队" : "正在生成视频";
  if (image) return image.status === "queued" ? "分镜图排队" : "正在生成分镜图";
  const shots = Array.isArray(data.shots) ? data.shots : [];
  if (data.clipStatus === "running") return "正在生成视频";
  if (data.clip) return "已生成视频";
  if (data.image) return "已生成 1 张分镜图";
  if (shots.length) return `已整理 ${shots.length} 镜`;
  return "选中后在右侧整理分镜";
}

function StoryboardNode({ id, data }: NodeProps<Node<{ label: string; duration?: number; shotCount?: number; shots?: StoryShot[]; image?: string; clip?: string; clipStatus?: string }>>) {
  const actions = useContext(CanvasActions);
  const edges = useEdges();
  const nodes = useNodes();
  const viralEdge = edges.find((edge) => edge.target === id && edge.targetHandle === "viral");
  const viralNode = nodes.find((node) => node.id === viralEdge?.source);
  const viralReady = Boolean(viralEdge && !VIRAL_EMPTY.has(String(viralNode?.data?.text || "").trim()));
  const taken = new Set(edges.filter((edge) => edge.target === id).map((edge) => edge.targetHandle));
  const [duration, setDuration] = useState(Number(data.duration) || 15);
  const [count, setCount] = useState(Number(data.shotCount) || 4);
  useEffect(() => { setDuration(Number(data.duration) || 15); actions?.rememberStory(id, { duration: Number(data.duration) || 15 }); }, [data.duration, id]);
  useEffect(() => { setCount(Number(data.shotCount) || 4); actions?.rememberStory(id, { shotCount: Number(data.shotCount) || 4 }); }, [data.shotCount, id]);
  const shots = Array.isArray(data.shots) ? data.shots : [];

  function commit(patch: Record<string, unknown>) {
    actions?.saveStory(id, patch);
  }

  return (
    <div className="node story">
      <div className="node-head"><span className="node-mark">镜</span><strong>分镜头生成</strong></div>
      <RemoveButton id={id} />
      <div className="story-fields nodrag nopan" onPointerDown={(event) => event.stopPropagation()}>
        <label>时长
          <input type="number" min={1} max={60} value={duration} onChange={(event) => { const next = Number(event.target.value); setDuration(next); actions?.rememberStory(id, { duration: next }); }} onBlur={() => commit({ duration })} />
        </label>
        <label>镜数
          <input type="number" min={1} max={12} value={viralReady ? shots.length || count : count} disabled={viralReady} title={viralReady ? "镜数按爆款分镜" : ""} onChange={(event) => { const next = Number(event.target.value); setCount(next); actions?.rememberStory(id, { shotCount: next }); }} onBlur={() => { if (!viralReady) commit({ shot_count: count }); }} />
        </label>
      </div>
      <ul className="story-ports">
        {STORY_PORTS.map((port) => (
          <li key={port.id} className={taken.has(port.id) ? "on" : ""}>{port.label}</li>
        ))}
      </ul>
      <p className="desc story-state">{storyState(id, data, actions?.jobs || [])}</p>
      <span className="story-out" style={{ top: 148 }}>分镜</span>
      <span className="story-out second" style={{ top: 176 }}>视频</span>
      {STORY_PORTS.map((port) => (
        <Handle key={port.id} type="target" id={port.id} position={Position.Left} className={`story-${port.id}`} style={{ top: port.top }} />
      ))}
      <Handle type="source" id="shots" position={Position.Right} className="story-shots" style={{ top: 154 }} />
      <Handle type="source" id="video" position={Position.Right} className="story-video" style={{ top: 182 }} />
    </div>
  );
}

const nodeTypes = { scene: SceneNode, asset: AssetNode, product: ProductNode, variant: VariantNode, slot: SlotNode, facts: FactsNode, voice: TextNode, note: TextNode, viral: TextNode, storyboard: StoryboardNode };

export default function App() {
  const [section, setSection] = useState<Section>("products");
  const [scenes, setScenes] = useState<Scene[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [sceneId, setSceneId] = useState("");
  const [productId, setProductId] = useState("");
  const [message, setMessage] = useState("");

  async function loadScenes() {
    const next = await request("/scenes?include_inactive=1");
    setScenes(next);
    return next as Scene[];
  }
  async function loadProducts() {
    const next = await request("/products");
    setProducts(next);
    return next as Product[];
  }
  async function refreshJobs() {
    try {
      setJobs(await request("/jobs"));
    } catch {
      /* 刷新失败时留着上一次的列表 */
    }
  }
  async function removeProduct(id: string) {
    if (!id || id === "new") return false;
    const name = products.find((item) => item.id === id)?.title || "未命名商品";
    if (!window.confirm(`删除「${name}」？这个商品的画布和资料会一起去掉。`)) return false;
    await request(`/products/${id}`, { method: "DELETE" });
    const next = await loadProducts();
    setProductId(next[0]?.id || "");
    setMessage("");
    return true;
  }

  useEffect(() => {
    loadScenes().then((next) => { if (next[0]) setSceneId((current) => current || next[0].id); }).catch((error: Error) => setMessage(error.message));
    loadProducts().catch(() => undefined);
    refreshJobs();
    const timer = window.setInterval(() => { refreshJobs(); }, 2000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!productId && products[0]) setProductId(products[0].id);
  }, [productId, products]);

  const selectedScene = scenes.find((scene) => scene.id === sceneId) ?? null;

  return (
    <div className="studio">
      <header className="topbar">
        <div className="brand">带货视频</div>
        <div className="mode">
          <button type="button" aria-current={section === "products" ? "page" : undefined} onClick={() => { setSection("products"); setMessage(""); loadProducts(); }}>商品</button>
          <button type="button" aria-current={section === "scenes" ? "page" : undefined} onClick={() => { setSection("scenes"); setMessage(""); }}>场景库</button>
          <button type="button" aria-current={section === "people" ? "page" : undefined} onClick={() => { setSection("people"); setMessage(""); }}>人物素材库</button>
          <button type="button" aria-current={section === "prompts" ? "page" : undefined} onClick={() => { setSection("prompts"); setMessage(""); loadProducts(); }}>提示词库</button>
          <button type="button" aria-current={section === "hits" ? "page" : undefined} onClick={() => { setSection("hits"); setMessage(""); loadProducts(); }}>爆款素材库</button>
          <button type="button" aria-current={section === "speech" ? "page" : undefined} onClick={() => { setSection("speech"); setMessage(""); loadProducts(); }}>口播审核</button>
        </div>
        <div className="top-status">夜间出片</div>
      </header>
      <div className="workspace">
        {section === "scenes" ? (
          <>
            <aside className="assets">
              <div className="asset-head"><strong>场景</strong><button type="button" onClick={() => { setSceneId("new"); setMessage(""); }}>新增</button></div>
              <div className="asset-grid">
                {scenes.length ? scenes.map((scene) => (
                  <button type="button" key={scene.id} className={scene.id === sceneId ? "asset-card current" : "asset-card"} onClick={() => { setSceneId(scene.id); setMessage(""); }}>
                    <img src={scene.image} alt="" />
                    <strong>{scene.name}</strong>
                    <span>{scene.active ? "使用中" : "已停用"}</span>
                  </button>
                )) : <p className="menu-empty">还没有场景。点新增，放一张图和一段描述词。</p>}
              </div>
            </aside>
            {sceneId === "new" || !selectedScene ? (
              <SceneCreate reload={loadScenes} onCreated={setSceneId} onError={setMessage} message={message} />
            ) : (
              <SceneDetail scene={selectedScene} reload={loadScenes} onError={setMessage} message={message} />
            )}
          </>
        ) : section === "people" ? (
          <PeopleLibrary onError={setMessage} message={message} />
        ) : section === "prompts" ? (
          <PromptLibrary products={products} productId={productId === "new" ? "" : productId} onSelect={(id) => { setProductId(id); setMessage(""); }} onError={setMessage} message={message} />
        ) : section === "hits" ? (
          <HitLibrary products={products} productId={productId === "new" ? "" : productId} onSelect={(id) => { setProductId(id); setMessage(""); }} onError={setMessage} message={message} />
        ) : section === "speech" ? (
          <SpeechLibrary products={products} productId={productId === "new" ? "" : productId} onSelect={(id) => { setProductId(id); setMessage(""); }} onError={setMessage} message={message} />
        ) : (
          <>
            <ProductRail products={products} jobs={jobs} productId={productId} onSelect={(id) => { setProductId(id); setMessage(""); }} onCreate={() => { setProductId("new"); setMessage(""); }} onDelete={() => removeProduct(productId).catch((error: Error) => setMessage(error.message))} exportable onNotice={setMessage} />
            {productId && productId !== "new" ? (
              <Board key={productId} productId={productId} jobs={jobs.filter((job) => job.product_id === productId)} refreshJobs={refreshJobs} scenes={scenes.filter((scene) => scene.active)} onError={setMessage} message={message} reloadScenes={loadScenes} onChanged={loadProducts} onDelete={() => removeProduct(productId)} />
            ) : (
              <>
                <NodeLibrary onAdd={() => undefined} disabled />
                <section className="viewer"><div className="viewer-stage"><div className="empty">左边选一个商品，画布就留在这里。点新增时，在右边填写链接。</div></div></section>
                <ProductCreate reload={loadProducts} onCreated={(id) => { setProductId(id); setMessage(""); }} onError={setMessage} message={message} />
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function SceneCreate({ reload, onCreated, onError, message }: { reload: () => Promise<Scene[]>; onCreated: (id: string) => void; onError: (text: string) => void; message: string }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  async function createScene(event: FormEvent) {
    event.preventDefault();
    if (!file) return onError("场景需要图片");
    const body = new FormData();
    body.set("name", name);
    body.set("description", description);
    body.set("image", file);
    try {
      const created = await request("/scenes", { method: "POST", body });
      setName("");
      setDescription("");
      setFile(null);
      setPreview("");
      if (fileInput.current) fileInput.current.value = "";
      onError("");
      await reload();
      onCreated(created.id);
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有保存");
    }
  }

  return (
    <>
      <section className="viewer">
        <div className="viewer-label">预览</div>
        <div className="viewer-stage">
          {preview ? <img src={preview} alt="" /> : <div className="empty">场景图会显示在这里</div>}
        </div>
      </section>
      <aside className="inspector">
        <h3>新增场景</h3>
        <p className="desc">一张图配一段描述词，写明取什么、不取什么。</p>
        {message ? <p className="notice">{message}</p> : null}
        <form onSubmit={createScene}>
          <label>名称<input value={name} onChange={(event) => setName(event.target.value)} required /></label>
          <label>描述词<textarea value={description} onChange={(event) => setDescription(event.target.value)} required placeholder="这张图取什么，不取什么" /></label>
          <label>图片<input ref={fileInput} type="file" accept="image/*" onChange={(event) => { const next = event.target.files?.[0] ?? null; setFile(next); setPreview(next ? URL.createObjectURL(next) : ""); }} required /></label>
          <div className="actions"><button className="primary" type="submit">保存</button></div>
        </form>
      </aside>
    </>
  );
}

function SceneDetail({ scene, reload, onError, message }: { scene: Scene; reload: () => Promise<Scene[]>; onError: (text: string) => void; message: string }) {
  const [name, setName] = useState(scene.name);
  const [description, setDescription] = useState(scene.description);
  const [file, setFile] = useState<File | null>(null);

  useEffect(() => {
    setName(scene.name);
    setDescription(scene.description);
    setFile(null);
  }, [scene.id, scene.name, scene.description, scene.image]);

  async function save(event: FormEvent) {
    event.preventDefault();
    const body = new FormData();
    body.set("name", name);
    body.set("description", description);
    body.set("active", scene.active ? "1" : "0");
    if (file) body.set("image", file);
    try {
      await request(`/scenes/${scene.id}`, { method: "PATCH", body });
      setFile(null);
      onError("");
      await reload();
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有保存");
    }
  }

  async function toggle() {
    const body = new FormData();
    body.set("active", scene.active ? "0" : "1");
    await request(`/scenes/${scene.id}`, { method: "PATCH", body });
    await reload();
  }

  return (
    <>
      <section className="viewer">
        <div className="viewer-label">{scene.name} · {scene.active ? "使用中" : "已停用"}</div>
        <div className="viewer-stage"><img src={scene.image} alt="" /></div>
      </section>
      <aside className="inspector">
        <h3>场景</h3>
        {message ? <p className="notice">{message}</p> : null}
        <form onSubmit={save}>
          <label>名称<input value={name} onChange={(event) => setName(event.target.value)} required /></label>
          <label>描述词<textarea value={description} onChange={(event) => setDescription(event.target.value)} required /></label>
          <label>换图片<input type="file" accept="image/*" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
          <div className="actions">
            <button className="primary" type="submit">保存修改</button>
            <button className="ghost" type="button" onClick={() => toggle().catch((error: Error) => onError(error.message))}>{scene.active ? "停用" : "启用"}</button>
          </div>
        </form>
      </aside>
    </>
  );
}

function ProductCreate({ reload, onCreated, onError, message }: { reload: () => Promise<Product[]>; onCreated: (id: string) => void; onError: (text: string) => void; message: string }) {
  const [url, setUrl] = useState("");
  async function createProduct(event: FormEvent) {
    event.preventDefault();
    try {
      const product = await request("/products", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
      setUrl("");
      onError("");
      await reload();
      onCreated(product.id);
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有创建");
    }
  }
  return (
    <aside className="inspector">
      <h3>新建商品</h3>
      <p className="desc">一条链接对应一个商品。创建后打开画布，再从节点库接场景和人物。</p>
      {message ? <p className="notice">{message}</p> : null}
      <form onSubmit={createProduct}>
        <label>商品链接<input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="可留空，稍后手填" /></label>
        <div className="actions"><button className="primary" type="submit">创建</button></div>
      </form>
    </aside>
  );
}

function NodeLibrary({ onAdd, hint, disabled, children }: { onAdd: (step: GraphKind) => void; hint?: string; disabled?: boolean; children?: ReactNode }) {
  return (
    <aside className="assets">
      <div className="asset-head"><strong>节点库</strong></div>
      <div className="asset-scroll">
        <p className="menu-empty">{disabled ? "先在左边选一个商品" : hint}</p>
        <div className="library">
          {NEXT_ITEMS.map((item) => (
            <button type="button" key={item.step} disabled={disabled} onClick={() => onAdd(item.step || "note")}>
              <span className="lib-mark">{item.mark || item.title.slice(0, 1)}</span>
              <span><strong>{item.title}</strong><em>{item.hint}</em></span>
            </button>
          ))}
        </div>
        {children}
      </div>
    </aside>
  );
}

function ImageLightbox({ images, index, onIndex, onClose, onPromote }: { images: string[]; index: number; onIndex: (index: number) => void; onClose: () => void; onPromote?: (file: File) => Promise<void> }) {
  const [note, setNote] = useState("");
  const [saved, setSaved] = useState<string[]>([]);
  const [promoting, setPromoting] = useState(false);
  const src = images[index] || "";
  const canPromote = Boolean(onPromote) && src.includes("/files/products/");

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowLeft" && index > 0) onIndex(index - 1);
      if (event.key === "ArrowRight" && index < images.length - 1) onIndex(index + 1);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [images.length, index, onClose, onIndex]);

  async function copyImage() {
    setNote("");
    const response = await fetch(src);
    if (!response.ok) throw new Error("没有读到图片");
    const blob = await response.blob();
    const png = blob.type === "image/png" ? blob : await pngBlob(blob);
    await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
    setNote("已复制，可以粘贴到别的地方。");
  }

  async function saveImage() {
    setNote("");
    const response = await fetch(src);
    if (!response.ok) throw new Error("没有读到图片");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `商品图-${index + 1}.${await imageExt(blob, src)}`;
    link.click();
    URL.revokeObjectURL(url);
    setNote("已开始下载。");
  }

  return (
    <div className="lightbox" onClick={onClose}>
      <div className="lightbox-bar" onClick={(event) => event.stopPropagation()}>
        <button type="button" className="ghost" disabled={index <= 0} onClick={() => onIndex(index - 1)}>上一张</button>
        <span>{index + 1} / {images.length}</span>
        <button type="button" className="ghost" disabled={index >= images.length - 1} onClick={() => onIndex(index + 1)}>下一张</button>
        <button type="button" className="primary" onClick={() => copyImage().catch(() => setNote("没有复制成功。"))}>复制图片</button>
        <button type="button" className="ghost" onClick={() => saveImage().catch(() => setNote("没有另存成功。"))}>另存为</button>
        {canPromote ? <button type="button" className="primary" disabled={promoting || saved.includes(src)} onClick={() => {
          setPromoting(true);
          setNote("");
          fetch(src).then(async (response) => {
            if (!response.ok) throw new Error("没有读到图片");
            const blob = await response.blob();
            const file = new File([blob], `白底图.${await imageExt(blob, src)}`, { type: blob.type || "image/jpeg" });
            await onPromote?.(file);
            setSaved((current) => current.includes(src) ? current : [...current, src]);
            setNote("已转到白底图，商品图还在。");
          }).catch(() => setNote("没有转成白底图。")).finally(() => setPromoting(false));
        }}>{saved.includes(src) ? "已转入" : promoting ? "正在转入" : "转为白底图"}</button> : null}
        <button type="button" className="ghost" onClick={onClose}>关闭</button>
      </div>
      <img src={src} alt={`商品图 ${index + 1}`} onClick={(event) => event.stopPropagation()} />
      {note ? <p className="lightbox-note" onClick={(event) => event.stopPropagation()}>{note}</p> : <p className="lightbox-note">点空白处或按 Esc 关闭。大图上可以右键复制或另存。</p>}
    </div>
  );
}

async function imageExt(blob: Blob, src: string) {
  const head = new Uint8Array(await blob.slice(0, 12).arrayBuffer());
  if (head[0] === 0xff && head[1] === 0xd8) return "jpg";
  if (head[0] === 0x89 && head[1] === 0x50) return "png";
  if (head[0] === 0x47 && head[1] === 0x49) return "gif";
  if (head[0] === 0x52 && head[8] === 0x57) return "webp";
  if (blob.type === "image/jpeg") return "jpg";
  if (blob.type === "image/webp") return "webp";
  if (blob.type === "image/png") return "png";
  const match = src.match(/\.([a-z0-9]+)(?:\?|$)/i);
  return match && match[1] !== "img" ? match[1].toLowerCase() : "jpg";
}

function pngBlob(blob: Blob) {
  return new Promise<Blob>((resolve, reject) => {
    const image = new Image();
    const url = URL.createObjectURL(blob);
    image.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth || 1;
      canvas.height = image.naturalHeight || 1;
      canvas.getContext("2d")?.drawImage(image, 0, 0);
      URL.revokeObjectURL(url);
      canvas.toBlob((out) => (out ? resolve(out) : reject(new Error("没有复制"))), "image/png");
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("没有打开图片"));
    };
    image.src = url;
  });
}

const WHITE_LEAD = "根据产品信息及上传的图片生成一张白底图";
const DETAIL_LEAD = "请先根据我提供的商品信息和图片，生成一组同一商品的多视角参考图，包括正面、背面、左右侧面、45度角和细节特写。所有图片必须保持商品的外形、比例、颜色、材质、图案、Logo、结构及配件完全一致，不得随意增加、删除或改变商品细节。多视角图生成完成后，将它们与原始商品图一起作为后续所有电商图片的商品身份参考，固定商品一致性，再开始生成正式的主图、详情页或A+图片。原图中无法确认的隐藏结构不要擅自编造。这次只输出一张图，把正面、背面、左侧面、右侧面、45度角和细节特写排在同一张纯白背景上，每个视角单独一格，格子之间和画面边缘都留干净白边。不要加标题、卖点、价格、水印、边框或场景。";
const REF_LIMIT = 3;

type WhiteShot = { id: string; src: string; prior?: boolean; badge?: string };

function initialPick(shots: WhiteShot[]) {
  const products = shots.filter((shot) => shot.badge === "商品图");
  const whites = shots.filter((shot) => shot.badge === "已有白底图");
  const picked: string[] = [];
  if (whites.length) picked.push(whites[whites.length - 1].id);
  for (const shot of [...products, ...whites]) {
    if (picked.length >= REF_LIMIT) break;
    if (!picked.includes(shot.id)) picked.push(shot.id);
  }
  return picked;
}

function joinedPrompt(lead: string, profile: string) {
  return `${lead}\n${profile.trim()}`;
}

function ImageSheetDialog({ title, action, profile, shots, initialPrompt, prepare, selectable, onClose, onZoom, onSubmit }: { title: string; action: string; profile: string; shots: WhiteShot[]; initialPrompt: string; prepare?: () => Promise<string>; selectable?: boolean; onClose: () => void; onZoom: (images: string[], index: number) => void; onSubmit: (prompt: string, assetIds: string[]) => Promise<void> }) {
  const [kept, setKept] = useState(shots);
  const [picked, setPicked] = useState<string[]>(() => (selectable ? initialPick(shots) : shots.map((shot) => shot.id)));
  const [prompt, setPrompt] = useState(initialPrompt);
  const [pendingPrompt, setPendingPrompt] = useState(Boolean(prepare));
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const prepareRef = useRef(prepare);
  const chosen = kept.filter((shot) => picked.includes(shot.id));
  const tooMany = !selectable && kept.length > REF_LIMIT;
  useEffect(() => {
    const load = prepareRef.current;
    if (!load) return;
    let alive = true;
    load().then((text) => {
      if (!alive) return;
      setPrompt(text);
      setPendingPrompt(false);
    }).catch((error: Error) => {
      if (!alive) return;
      setProblem(error.message || "提示词没有整理好");
      setPendingPrompt(false);
    });
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy && !document.querySelector(".lightbox")) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);
  return (
    <div className="sheet-dialog" onClick={() => { if (!busy) onClose(); }}>
      <div className="sheet-panel nodrag nopan nowheel" onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="white-sheet-title">
        <div className="sheet-head">
          <h3 id="white-sheet-title">{title}</h3>
          <button type="button" className="ghost" disabled={busy} onClick={onClose}>关闭</button>
        </div>
        <p className="desc">{selectable ? "商品图和白底图一起最多选 3 张。点图片看大图，点「选用」决定这次用哪几张。" : "最多留 3 张。点图片看大图，右上角拿掉的只是这次不拿去参考，商品里的原图还在。"}</p>
        {tooMany ? <p className="notice">现在有 {kept.length} 张，请删掉多余的，最多留 3 张。</p> : null}
        {selectable ? <p className="desc">已选 {chosen.length} / {REF_LIMIT} 张</p> : null}
        {kept.length ? (
          <div className="sheet-photos">
            {kept.map((shot, index) => {
              const on = picked.includes(shot.id);
              return (
                <figure key={shot.id} className={selectable ? (on ? "is-on" : "is-off") : undefined}>
                  <button type="button" className="sheet-zoom" onClick={() => onZoom(kept.map((item) => item.src), index)}>
                    <img src={shot.src} alt={shot.badge || `参考图 ${index + 1}`} />
                  </button>
                  {shot.badge ? <span className="sheet-badge">{shot.badge}</span> : null}
                  {selectable ? <button type="button" className="sheet-pick" aria-pressed={on} onClick={() => setPicked((current) => {
                    if (current.includes(shot.id)) return current.filter((id) => id !== shot.id);
                    if (current.length >= REF_LIMIT) {
                      setProblem("商品图和白底图一起最多选 3 张");
                      return current;
                    }
                    setProblem("");
                    return [...current, shot.id];
                  })}>{on ? "已选" : "选用"}</button> : null}
                  <button type="button" className="sheet-remove" aria-label="删除这张参考图" title="删除这张参考图" onClick={() => { setKept((current) => current.filter((item) => item.id !== shot.id)); setPicked((current) => current.filter((id) => id !== shot.id)); }}><span aria-hidden="true"></span></button>
                </figure>
              );
            })}
          </div>
        ) : <p className="film-index">至少留一张参考图</p>}
        <label>提示词<textarea className="prompt" value={pendingPrompt ? "正在根据产品信息整理提示词…" : prompt} readOnly={pendingPrompt} onChange={(event) => setPrompt(event.target.value)} /></label>
        <label>产品信息<textarea readOnly value={profile} /></label>
        {problem ? <p className="notice">{problem}</p> : null}
        <div className="actions">
          <button type="button" className="ghost" disabled={busy} onClick={onClose}>取消</button>
          <button type="button" className="primary" disabled={busy || pendingPrompt || tooMany || !chosen.length || !prompt.trim()} onClick={() => {
            setBusy(true);
            setProblem("");
            onSubmit(prompt.trim(), chosen.map((shot) => shot.id)).catch((error: Error) => {
              setProblem(error.message || "没有生成");
              setBusy(false);
            });
          }}>{busy ? "正在生成" : action}</button>
        </div>
      </div>
    </div>
  );
}

function Board({ productId, jobs, refreshJobs, scenes, onError, message, reloadScenes, onChanged, onDelete }: { productId: string; jobs: Job[]; refreshJobs: () => Promise<void>; scenes: Scene[]; onError: (text: string) => void; message: string; reloadScenes: () => Promise<Scene[]>; onChanged: () => Promise<Product[]>; onDelete: () => Promise<boolean> }) {
  const [product, setProduct] = useState<Product | null>(null);
  const [people, setPeople] = useState<Person[]>([]);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [bundle, setBundle] = useState<VariantBundle | null>(null);
  const [picker, setPicker] = useState<"" | "scene" | "model" | "facts" | "text" | "viral" | "product" | "storyboard">("");
  const [focusId, setFocusId] = useState("");
  const [scheduler, setScheduler] = useState({ open: 0, probed: 0 });
  const [boundPrompt, setBoundPrompt] = useState<PromptItem | null>(null);
  const [boundHits, setBoundHits] = useState<HitItem[]>([]);
  const [templates, setTemplates] = useState<TemplateItem[]>([]);
  const [preview, setPreview] = useState<{ images: string[]; index: number; promote?: boolean } | null>(null);
  const [whiteSheet, setWhiteSheet] = useState<{ kind: "white" | "detail" | "usage"; title: string; action: string; profile: string; shots: WhiteShot[]; prompt: string } | null>(null);
  const [usagePreparing, setUsagePreparing] = useState(false);
  const nodesRef = useRef(nodes);
  const edgesRef = useRef(edges);
  const storyRef = useRef<Record<string, { duration: number; shotCount: number }>>({});
  nodesRef.current = nodes;
  edgesRef.current = edges;

  async function loadPeople() {
    setPeople(await request("/people"));
  }

  async function loadBoard() {
    const board = await request(`/boards/${productId}`);
    setProduct(board.product);
    setNodes(board.nodes);
    setEdges(board.edges);
    setScheduler(await request("/scheduler"));
    const [promptRows, hitRows] = await Promise.all([request("/prompts").catch(() => []), request("/hits").catch(() => [])]);
    const promptItems = promptRows as PromptItem[];
    setBoundPrompt(promptItems.find((item) => item.product_id === productId && item.origin === "generated") ?? promptItems.find((item) => item.product_id === productId) ?? null);
    setBoundHits((hitRows as HitItem[]).filter((item) => item.product_id === productId));
    setTemplates(await request("/templates").catch(() => []));
  }

  async function setTemplate(variantId: string, templateId: string) {
    await request(`/variants/${variantId}/template`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ template_id: templateId }),
    });
    await loadBoard();
    if (bundle?.variant.id === variantId) setBundle(await request(`/variants/${variantId}`));
  }

  useEffect(() => {
    setBundle(null);
    loadPeople().catch((error: Error) => onError(error.message));
    loadBoard().catch((error: Error) => onError(error.message));
  }, [productId]);

  const seenJobs = useRef<string | null>(null);
  useEffect(() => {
    const sig = jobs.map((job) => `${job.id}:${job.status}:${job.error}`).join("|");
    if (seenJobs.current === null) {
      seenJobs.current = sig;
      return;
    }
    if (seenJobs.current === sig) return;
    seenJobs.current = sig;
    loadBoard().catch(() => undefined);
  }, [jobs]);

  useEffect(() => {
    if (!product || product.status !== "collecting" && product.status !== "fetching") return;
    const timer = setInterval(() => { loadBoard().then(() => onChanged()).catch(() => undefined); }, 2000);
    return () => clearInterval(timer);
  }, [product?.status]);

  const filmingIds = nodes
    .filter((node) => {
      const status = (node.data as { status?: string } | undefined)?.status;
      return node.type === "variant" && (status === "queued" || status === "running");
    })
    .map((node) => node.id)
    .join(",");
  useEffect(() => {
    if (!filmingIds) return;
    const ids = filmingIds.split(",");
    let stop = false;
    const timer = setInterval(() => {
      Promise.all(ids.map(async (id) => [id, await request(`/variants/${id}`) as VariantBundle] as const))
        .then((updates) => {
          if (stop) return;
          const byId = new Map(updates);
          setNodes((current) => current.map((node) => {
            const next = byId.get(node.id);
            const status = (node.data as { status?: string } | undefined)?.status;
            if (!next || status === next.variant.status) return node;
            return { ...node, data: { ...node.data, status: next.variant.status } };
          }));
          setBundle((current) => (current ? byId.get(current.variant.id) ?? current : current));
        })
        .catch(() => undefined);
    }, 2000);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, [filmingIds]);

  async function persist(nextNodes = nodesRef.current, nextEdges = edgesRef.current) {
    const board = await request(`/boards/${productId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nodes: nextNodes, edges: nextEdges }),
    });
    setNodes(board.nodes);
    setEdges(board.edges);
    setProduct(board.product);
  }

  async function addScene(sceneId: string) {
    const board = await request(`/boards/${productId}/scenes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scene_id: sceneId }),
    });
    setNodes(board.nodes);
    setEdges(board.edges);
    setPicker("");
  }

  async function addNode(step: GraphKind, sourceId: string) {
    const before = new Set(nodesRef.current.map((node) => node.id));
    const board = await request(`/boards/${productId}/nodes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: step, source: sourceId }),
    });
    setNodes(board.nodes);
    setEdges(board.edges);
    setBundle(null);
    const created = board.nodes.find((node: { id: string; type?: string; data?: { step?: string } }) => !before.has(node.id));
    if (!created) return;
    setFocusId(created.id);
    if (created.type === "variant") {
      setPicker("");
      await openVariant(created.id);
      return;
    }
    if (created.type === "slot") setPicker(created.data?.step === "model" ? "model" : "scene");
    else if (created.type === "facts") setPicker("facts");
    else if (created.type === "viral") setPicker("viral");
    else if (created.type === "storyboard") setPicker("storyboard");
    else if (created.type === "voice" || created.type === "note") setPicker("text");
  }

  async function saveNodeText(event: FormEvent) {
    event.preventDefault();
    const text = new FormData(event.currentTarget as HTMLFormElement).get("text");
    const board = await request(`/boards/${productId}/nodes/${encodeURIComponent(focusId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: String(text || "") }),
    });
    setNodes(board.nodes);
    setEdges(board.edges);
  }

  async function fillScene(sceneId: string) {
    const board = await request(`/boards/${productId}/nodes/${encodeURIComponent(focusId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scene_id: sceneId }),
    });
    setNodes(board.nodes);
    setEdges(board.edges);
    setPicker("");
  }

  async function removeNode(id: string) {
    const board = await request(`/boards/${productId}/nodes/${encodeURIComponent(id)}`, { method: "DELETE" });
    setNodes(board.nodes);
    setEdges(board.edges);
    setBundle(null);
    setPicker("");
    setFocusId((current) => (current === id ? "" : current));
  }

  async function uploadWhites(list: File[]) {
    const body = new FormData();
    list.forEach((file) => body.append("images", file));
    const board = await request(`/products/${productId}/white-images`, { method: "POST", body });
    setNodes(board.nodes);
    setEdges(board.edges);
  }

  async function removeWhite(assetId: string) {
    const board = await request(`/products/${productId}/white-images/${encodeURIComponent(assetId)}`, { method: "DELETE" });
    setNodes(board.nodes);
    setEdges(board.edges);
  }

  async function removeUsage(assetId: string) {
    const board = await request(`/products/${productId}/usage-images/${encodeURIComponent(assetId)}`, { method: "DELETE" });
    setNodes(board.nodes);
    setEdges(board.edges);
  }

  function productCard() {
    return nodesRef.current.find((node) => node.type === "product")?.data || {};
  }

  function pairedShots(ids: unknown, srcs: unknown, badgeFor?: (id: string) => string) {
    const keys = Array.isArray(ids) ? ids.map(String) : [];
    const images = Array.isArray(srcs) ? srcs.map(String) : [];
    return keys.map((id, index) => ({ id, src: images[index] || "", badge: badgeFor ? badgeFor(id) : "" })).filter((shot) => shot.id && shot.src);
  }

  function openWhiteSheet() {
    const data = productCard();
    const profile = String(data.profile || "").trim();
    if (!profile) return;
    const shots = [
      ...pairedShots(data.photoIds, data.images).map((shot) => ({ ...shot, badge: "商品图" })),
      ...pairedShots(data.whiteIds, data.whites).map((shot) => ({ ...shot, badge: "已有白底图" })),
    ];
    setWhiteSheet({
      kind: "white",
      title: Array.isArray(data.whiteIds) && data.whiteIds.length ? "重新生成白底图" : "生成白底图",
      action: Array.isArray(data.whiteIds) && data.whiteIds.length ? "重新生成" : "生成白底图",
      profile,
      shots,
      prompt: joinedPrompt(WHITE_LEAD, profile),
    });
  }

  function openDetailSheet() {
    const data = productCard();
    const profile = String(data.profile || "").trim();
    const whites = pairedShots(data.whiteIds, data.whites);
    if (!profile || !whites.length) return;
    const shots = [
      ...pairedShots(data.photoIds, data.images).map((shot) => ({ ...shot, badge: "商品图" })),
      ...whites.map((shot) => ({ ...shot, badge: "已有白底图" })),
    ];
    const again = Array.isArray(data.detailIds) && data.detailIds.length > 0;
    setWhiteSheet({
      kind: "detail",
      title: again ? "重新生成细节图" : "生成细节图",
      action: again ? "重新生成" : "生成细节图",
      profile,
      shots,
      prompt: joinedPrompt(DETAIL_LEAD, profile),
    });
  }

  async function openUsageSheet() {
    const data = productCard();
    const profile = String(data.profile || "").trim();
    const shots = pairedShots(data.detailIds, data.details, () => "细节图");
    if (!profile || !shots.length || usagePreparing) return;
    setUsagePreparing(true);
    try {
      const prompt = await prepareUsagePrompt();
      setWhiteSheet({
        kind: "usage",
        title: "生成使用场景图",
        action: "生成使用场景图",
        profile,
        shots,
        prompt,
      });
    } catch (error) {
      onError(error instanceof Error ? error.message : "提示词没有整理好");
    } finally {
      setUsagePreparing(false);
    }
  }

  async function prepareUsagePrompt() {
    const body = await request(`/products/${productId}/usage-images/prompt`, { method: "POST" });
    return String(body.prompt || "");
  }

  async function submitWhiteSheet(prompt: string, assetIds: string[]) {
    const path = whiteSheet?.kind === "detail" ? "detail-images/generate" : whiteSheet?.kind === "usage" ? "usage-images" : "white-images/generate";
    const board = await request(`/products/${productId}/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, asset_ids: assetIds }),
    });
    setNodes(board.nodes);
    setEdges(board.edges);
    setWhiteSheet(null);
    onError("");
    await refreshJobs();
  }

  async function useViral(hitId: string) {
    const board = await request(`/boards/${productId}/nodes/${encodeURIComponent(focusId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hit_id: hitId }),
    });
    setNodes(board.nodes);
    setEdges(board.edges);
  }

  async function usePerson(personId: string) {
    const board = await request(`/products/${productId}/cast`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ person_id: personId, node_id: focusId }),
    });
    setNodes(board.nodes);
    setEdges(board.edges);
  }

  async function createPerson(event: FormEvent) {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const body = new FormData(form);
    if (!body.get("image")) return onError("人物需要图片");
    try {
      await request("/people", { method: "POST", body });
      form.reset();
      onError("");
      await loadPeople();
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有保存人物");
    }
  }

  async function openVariant(id: string) {
    setBundle(await request(`/variants/${id}`));
  }

  async function saveFacts(event: FormEvent) {
    event.preventDefault();
    if (!product) return;
    await request(`/products/${productId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: product.title, price: product.price, points: product.points }),
    });
    await loadBoard();
  }

  async function addReference(url: string) {
    const body = new FormData();
    body.set("url", url);
    await request(`/products/${productId}/reference`, { method: "POST", body });
    await loadBoard();
  }

  const productNode = nodes.find((node) => node.type === "product");
  const productImages = Array.isArray(productNode?.data?.images) ? productNode.data.images.map(String) : [];
  const usedScenes = new Set(nodes.filter((node) => node.type === "scene").map((node) => String(node.data?.sceneId || "")));
  const usedPeople = new Set(nodes.filter((node) => node.type === "asset" && node.data?.kind === "person").map((node) => String(node.data?.personId || "")));
  const fetching = product?.status === "collecting" || product?.status === "fetching";
  const focusNode = nodes.find((node) => node.id === focusId);
  const focusScene = focusNode?.type === "scene" ? String(focusNode.data?.sceneId || "") : "";
  return (
    <>
      <NodeLibrary hint={focusId && focusId !== productNode?.id ? "加到当前选中的卡片后面" : "加到商品卡后面"} onAdd={(step) => addNode(step, focusId || productNode?.id || "").catch((error: Error) => onError(error.message))} />
      <section className="viewer">
        <div className="viewer-label">{product?.title || "未命名商品"}{product ? ` · ${statusText(product.status)}` : ""} · 一个商品 {productImages.length} 张图</div>
        <div className="canvas">
          <CanvasActions.Provider value={{ remove: (id) => removeNode(id).catch((error: Error) => onError(error.message)), templates, setTemplate: (id, templateId) => setTemplate(id, templateId).catch((error: Error) => onError(error.message)), openImages: (images, index, promote) => setPreview({ images, index, promote }), uploadWhites: async (files) => { try { await uploadWhites(files); onError(""); } catch (error) { onError(error instanceof Error ? error.message : "没有上传"); } }, removeWhite: (assetId) => removeWhite(assetId).catch((error: Error) => onError(error.message)), openUsageSheet: () => { onError(""); openUsageSheet(); }, usagePreparing, removeUsage: (assetId) => removeUsage(assetId).catch((error: Error) => onError(error.message)), openWhiteSheet: () => { onError(""); openWhiteSheet(); }, openDetailSheet: () => { onError(""); openDetailSheet(); }, saveStory: (id, patch) => { request(`/boards/${productId}/nodes/${encodeURIComponent(id)}/storyboard`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }).then((board) => { setNodes(board.nodes); setEdges(board.edges); }).catch((error: Error) => onError(error.message)); }, rememberStory: (id, patch) => { const prev = storyRef.current[id] || { duration: 15, shotCount: 4 }; storyRef.current[id] = { ...prev, ...patch }; }, readStory: (id) => storyRef.current[id] || { duration: 15, shotCount: 4 }, jobs, refreshJobs }}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            deleteKeyCode={["Delete", "Backspace"]}
            onNodesDelete={(deleted) => {
              if (deleted.some((node) => node.type === "product")) {
                onDelete().then((removed) => { if (!removed) loadBoard().catch(() => undefined); }).catch((error: Error) => { onError(error.message); loadBoard().catch(() => undefined); });
                return;
              }
              const ids = deleted.map((node) => node.id);
              ids.reduce((chain, id) => chain.then(() => removeNode(id)), Promise.resolve()).catch((error: Error) => { onError(error.message); loadBoard().catch(() => undefined); });
            }}
            isValidConnection={(connection) => storyConnectionOk(nodesRef.current, edgesRef.current, connection)}
            onConnect={(connection: Connection) => {
              if (!storyConnectionOk(nodesRef.current, edgesRef.current, connection)) return;
              const handle = connection.targetHandle || "link";
              const next = [...edgesRef.current, { ...connection, id: `${connection.source}-${connection.target}-${handle}`, type: "smoothstep", style: { stroke: STORY_COLORS[handle] || "#b7c0d0", strokeWidth: 1.8 } }];
              setEdges(next);
              persist(nodesRef.current, next).catch((error: Error) => onError(error.message));
            }}
            onNodeDragStop={() => persist().catch((error: Error) => onError(error.message))}
            onPaneClick={() => { setFocusId(""); setPicker(""); setBundle(null); }}
            onNodeClick={(_event, node) => {
              setFocusId(node.id);
              if (node.type === "product") { setBundle(null); setPicker("product"); return; }
              if (node.type === "slot") { setBundle(null); setPicker(node.data?.step === "model" ? "model" : "scene"); return; }
              if (node.type === "facts") { setBundle(null); setPicker("facts"); return; }
              if (node.type === "scene") { setBundle(null); setPicker("scene"); return; }
              if (node.type === "voice" || node.type === "note") { setBundle(null); setPicker("text"); return; }
              if (node.type === "viral") { setBundle(null); setPicker("viral"); return; }
              if (node.type === "storyboard") { setBundle(null); setPicker("storyboard"); return; }
              if (node.type === "asset" && node.data?.kind === "person") { setBundle(null); setPicker("model"); return; }
              if (node.type === "variant") openVariant(node.id).catch((error: Error) => onError(error.message));
            }}
            nodeTypes={nodeTypes}
            colorMode="light"
            fitView
            defaultEdgeOptions={{ type: "smoothstep", style: { stroke: "#b7c0d0", strokeWidth: 1.6 } }}
          >
            <Background gap={22} color="#d5dbe6" />
            <Controls />
            <MiniMap pannable zoomable />
          </ReactFlow>
          </CanvasActions.Provider>
        </div>
        {whiteSheet ? <ImageSheetDialog title={whiteSheet.title} action={whiteSheet.action} profile={whiteSheet.profile} shots={whiteSheet.shots} initialPrompt={whiteSheet.prompt} selectable={whiteSheet.kind === "white" || whiteSheet.kind === "detail"} onClose={() => setWhiteSheet(null)} onZoom={(images, index) => setPreview({ images, index })} onSubmit={submitWhiteSheet} /> : null}
        {preview ? <ImageLightbox images={preview.images} index={preview.index} onIndex={(index) => setPreview({ images: preview.images, index, promote: preview.promote })} onClose={() => setPreview(null)} onPromote={preview.promote ? async (file) => { await uploadWhites([file]); } : undefined} /> : null}
      </section>
      <aside className="inspector">
        {message ? <p className="notice">{message}</p> : null}
        <JobStrip jobs={jobs} onDismiss={(id) => { request(`/jobs/${id}/dismiss`, { method: "POST" }).then(() => refreshJobs()).catch((error: Error) => onError(error.message)); }} />
        <ProductRefresh productId={productId} busy={fetching} onDone={async () => { await loadBoard(); await onChanged(); }} onError={onError} />
          {bundle ? <VariantEditor bundle={bundle} templates={templates} onChange={setBundle} onBoard={loadBoard} onTemplate={(templateId) => setTemplate(bundle.variant.id, templateId)} onTemplates={async () => setTemplates(await request("/templates"))} onError={onError} onBack={() => setBundle(null)} /> : picker === "scene" ? (
            <>
              {focusScene ? <div className="actions"><button className="ghost" type="button" onClick={() => addScene(focusScene).catch((error: Error) => onError(error.message))}>再做一版</button></div> : null}
              <ScenePicker scenes={scenes} used={usedScenes} onPick={(id) => (focusId ? fillScene(id) : addScene(id)).catch((error: Error) => onError(error.message))} onCreated={async (id) => { await reloadScenes(); if (focusId) await fillScene(id); else await addScene(id); }} onError={onError} />
            </>
          ) : picker === "text" ? (
            <form onSubmit={(event) => saveNodeText(event).catch((error: Error) => onError(error.message))}>
              <h3>{nodes.find((node) => node.id === focusId)?.type === "note" ? "自定义" : "口播"}</h3>
              <p className="desc">内容自己写。口播里的数字必须能在卖点、标题或价格里找到。</p>
              <label>内容<textarea name="text" defaultValue={String(nodes.find((node) => node.id === focusId)?.data?.text || "")} placeholder="写这一段要说的话，或任何补充" /></label>
              <div className="actions"><button className="primary" type="submit">保存</button></div>
            </form>
          ) : picker === "viral" ? (
            <ViralPicker hits={boundHits.filter((item) => item.kind === "video")} currentId={String(focusNode?.data?.hitId || "")} onPick={(id) => useViral(id).catch((error: Error) => onError(error.message))} />
          ) : picker === "storyboard" && focusNode?.type === "storyboard" ? (
            <StoryboardPanel productId={productId} node={focusNode} nodes={nodes} edges={edges} onBoard={(board) => { setNodes(board.nodes); setEdges(board.edges); setProduct(board.product); }} onError={onError} />
          ) : picker === "product" ? (
            <ProductSheet product={product} />
          ) : picker === "facts" ? (
            <FactsForm product={product} onChange={setProduct} onSubmit={saveFacts} />
          ) : picker === "model" ? (
            <ModelPicker people={people} used={usedPeople} onPick={(id) => usePerson(id).catch((error: Error) => onError(error.message))} onCreate={createPerson} onClear={() => request(`/products/${productId}/cast`, { method: "DELETE" }).then((board) => { setNodes(board.nodes); setEdges(board.edges); }).catch((error: Error) => onError(error.message))} />
          ) : (
            <>
              <h3>常规</h3>
              <p className="desc">{focusId && focusId !== productNode?.id ? "左侧节点会接到当前选中的卡片后面。点空白处，改接到商品卡。" : "从左侧节点库往下接。选中一张卡片后，新节点会接在那张后面。"}</p>
              <h3>对应到这个商品</h3>
              <p className="desc">提示词：{boundPrompt ? `${boundPrompt.name}${boundPrompt.scene_name ? ` · 场景 ${boundPrompt.scene_name}` : ""}` : "还没有"}</p>
              <p className="desc">爆款素材：{boundHits.length ? boundHits.map((item) => item.name).join("、") : "还没有"}</p>
              <h3>参考视频</h3>
              <ReferenceBox onAdd={addReference} />
              <h3>夜间队列</h3>
              <p className="desc">队列{scheduler.open ? "开着" : "关着"}，账单{scheduler.probed ? "已确认是 0" : "还没确认"}。确认后才会在夜间出片。</p>
              <button className="ghost" type="button" onClick={() => request("/scheduler/mark-probed", { method: "POST" }).then(setScheduler)}>账单已确认为 0</button>
            </>
          )}
        </aside>
    </>
  );
}

function StoryboardPanel({ productId, node, nodes, edges, onBoard, onError }: { productId: string; node: Node; nodes: Node[]; edges: Edge[]; onBoard: (board: { nodes: Node[]; edges: Edge[]; product: Product }) => void; onError: (text: string) => void }) {
  const actions = useContext(CanvasActions);
  const data = (node.data || {}) as { duration?: number; shotCount?: number; selectedIds?: string[] | null; shots?: StoryShot[]; image?: string; error?: string; videoPrompt?: string; videoError?: string; clip?: string; clipStatus?: string; clipError?: string; clipModel?: string };
  const links = edges.filter((edge) => edge.target === node.id);
  const linked = {
    product: links.some((edge) => edge.targetHandle === "product"),
    viral: links.some((edge) => edge.targetHandle === "viral"),
    person: links.some((edge) => edge.targetHandle === "person"),
    scene: links.some((edge) => edge.targetHandle === "scene"),
  };
  const wired = linked.product || linked.viral || linked.person || linked.scene;
  const viralNode = nodes.find((item) => item.id === links.find((edge) => edge.targetHandle === "viral")?.source);
  const viralReady = Boolean(linked.viral && !VIRAL_EMPTY.has(String(viralNode?.data?.text || "").trim()));
  const candidates = storyCandidates(nodes, edges, node.id);
  const [picked, setPicked] = useState<string[] | null>(Array.isArray(data.selectedIds) ? data.selectedIds.map(String) : null);
  const [rows, setRows] = useState<StoryShot[]>(Array.isArray(data.shots) ? data.shots : []);
  const [busy, setBusy] = useState("");
  const shotKey = JSON.stringify(data.shots || []);
  const pickKey = JSON.stringify(data.selectedIds ?? null);
  useEffect(() => { setPicked(Array.isArray(data.selectedIds) ? data.selectedIds.map(String) : null); }, [node.id, pickKey]);
  useEffect(() => { setRows(Array.isArray(data.shots) ? data.shots : []); }, [node.id, shotKey]);
  const chosen = new Set((picked ?? candidates.map((item) => item.id)).filter((id) => candidates.some((item) => item.id === id)));
  const imageJob = (actions?.jobs || []).find((job) => job.kind === "storyboard" && job.node_id === node.id && jobOpen(job));
  const videoJob = (actions?.jobs || []).find((job) => job.kind === "storyboard_video" && job.node_id === node.id && jobOpen(job));

  async function save(patch: Record<string, unknown>) {
    const board = await request(`/boards/${productId}/nodes/${encodeURIComponent(node.id)}/storyboard`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    onBoard(board);
  }

  async function draft() {
    setBusy("draft");
    onError("");
    try {
      const board = await request(`/boards/${productId}/nodes/${encodeURIComponent(node.id)}/storyboard/draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duration: actions?.readStory(node.id).duration || data.duration || 15, shot_count: actions?.readStory(node.id).shotCount || data.shotCount || 4, selected_ids: [...chosen] }),
      });
      onBoard(board);
    } catch (error) {
      onError(error instanceof Error ? error.message : "分镜没有整理好");
    } finally {
      setBusy("");
    }
  }

  async function generate() {
    setBusy("generate");
    onError("");
    try {
      const board = await request(`/boards/${productId}/nodes/${encodeURIComponent(node.id)}/storyboard/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duration: actions?.readStory(node.id).duration || data.duration || 15, shot_count: actions?.readStory(node.id).shotCount || data.shotCount || rows.length || 4, selected_ids: [...chosen], shots: rows }),
      });
      onBoard(board);
      await actions?.refreshJobs();
    } catch (error) {
      onError(error instanceof Error ? error.message : "分镜图没有生成");
    } finally {
      setBusy("");
    }
  }

  async function film() {
    setBusy("video");
    onError("");
    try {
      const board = await request(`/boards/${productId}/nodes/${encodeURIComponent(node.id)}/storyboard/video`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duration: actions?.readStory(node.id).duration || data.duration || 15, selected_ids: [...chosen], shots: rows }),
      });
      onBoard(board);
      await actions?.refreshJobs();
    } catch (error) {
      onError(error instanceof Error ? error.message : "视频没有生成");
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    if (data.clipStatus !== "running") return;
    const timer = setInterval(() => {
      request(`/boards/${productId}`).then(onBoard).catch(() => undefined);
    }, 2000);
    return () => clearInterval(timer);
  }, [data.clipStatus, productId, node.id]);

  function toggle(id: string) {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    const list = [...next];
    setPicked(list);
    save({ selected_ids: list }).catch((error: Error) => onError(error.message));
  }

  return (
    <div className="story-panel">
      <h3>确认分镜</h3>
      <p className="desc">先核对素材和每一镜的提示词。确认后只生成一张图，每一镜各占一格。时长和镜数写在节点上。</p>
      <p className="desc">已接 {[["商品", linked.product], ["爆款", linked.viral], ["模特", linked.person], ["场景", linked.scene]].filter((item) => item[1]).map((item) => item[0]).join("、") || "还没有"}。时长 {data.duration || 15} 秒。{viralReady ? "镜数按爆款。" : `镜数 ${data.shotCount || 4}。`}</p>
      <h3>共用素材</h3>
      {candidates.length ? (
        <div className="story-picks">
          {candidates.map((item) => (
            <label key={item.id} className="story-pick">
              <input type="checkbox" checked={chosen.has(item.id)} onChange={() => toggle(item.id)} />
              <img src={item.src} alt={item.badge} />
              <span>{item.badge}</span>
            </label>
          ))}
        </div>
      ) : <p className="desc">{wired ? "接上的节点里还没有白底图、细节图、使用场景图、模特或场景。" : "先把素材接到这个节点的输入口。"}</p>}
      <p className="desc">勾中的图会全部送给每一镜。一张都不勾，就只留下提示词，不生图。</p>
      <div className="actions">
        <button className="primary" type="button" disabled={!wired || Boolean(busy)} onClick={() => draft()}>{busy === "draft" ? "正在整理" : "整理分镜"}</button>
      </div>
      {rows.length ? (
        <>
          <h3>每一镜</h3>
          {rows.map((row, index) => (
            <div key={`${row.ordinal}-${index}`} className="story-shot">
              <span className="kind">第 {row.ordinal} 镜 · {row.seconds} 秒</span>
              <textarea
                value={row.prompt}
                onChange={(event) => setRows((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, prompt: event.target.value } : item))}
                onBlur={(event) => {
                  const next = rows.map((item, itemIndex) => itemIndex === index ? { ...item, prompt: event.target.value } : item);
                  setRows(next);
                  save({ shots: next }).catch((error: Error) => onError(error.message));
                }}
              />
            </div>
          ))}
          <div className="actions">
            <button className="primary" type="button" disabled={!chosen.size || Boolean(busy) || Boolean(imageJob) || Boolean(videoJob)} title={!chosen.size ? "先勾选参考图" : imageJob || videoJob ? "这一项已经提交" : ""} onClick={() => generate()}>{imageJob ? (imageJob.status === "queued" ? "分镜图排队" : "正在生成分镜图") : busy === "generate" ? "正在提交" : "生成分镜图"}</button>
          </div>
          {data.image ? (
            <>
              <img className="story-sheet" src={data.image} alt="分镜图" />
              <p className="desc">生成视频会把勾选的素材、每一镜提示词和这张分镜图交给 Seedance 2.0 Fast，480p，竖屏，不配声音。</p>
              <div className="actions">
                <button className="primary" type="button" disabled={Boolean(busy) || Boolean(videoJob) || Boolean(imageJob) || data.clipStatus === "running"} onClick={() => film()}>{videoJob ? (videoJob.status === "queued" ? "分镜视频排队" : "正在生成视频") : data.clipStatus === "running" || busy === "video" ? "正在生成视频" : data.clip ? "再生成一次" : "生成视频"}</button>
              </div>
              {data.clip ? <video className="story-clip" src={data.clip} controls /> : null}
              {data.clipError ? <p className="notice">{data.clipError}</p> : null}
            </>
          ) : null}
          {data.error ? <p className="notice">{data.error}</p> : null}
          {data.videoPrompt ? (
            <>
              <h3>视频提示词</h3>
              <textarea readOnly value={data.videoPrompt} />
            </>
          ) : null}
          {data.videoError ? <p className="notice">{data.videoError}</p> : null}
        </>
      ) : <p className="desc">点「整理分镜」后，这里按每一镜列出提示词。</p>}
    </div>
  );
}

function ProductRefresh({ productId, busy, onDone, onError }: { productId: string; busy: boolean; onDone: () => Promise<void>; onError: (text: string) => void }) {
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [saving, setSaving] = useState(false);

  async function refresh(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      await request(`/products/${productId}/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      setUrl("");
      setOpen(false);
      onError("");
      await onDone();
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有更新");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="product-refresh">
      <div className="actions">
        <button className="ghost" type="button" onClick={() => setOpen((value) => !value)}>{open ? "收起" : "更新产品资料"}</button>
      </div>
      {open ? (
        <form onSubmit={(event) => refresh(event)}>
          <p className="desc">贴 Amazon 或 1688 的商品链接。抓取方式和新建一样，完成后会盖掉现在的标题、卖点和产品图。白底图会留着。</p>
          <label>产品链接<input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://" required /></label>
          <div className="actions"><button className="primary" type="submit" disabled={saving || busy}>{saving || busy ? "正在更新" : "开始更新"}</button></div>
        </form>
      ) : null}
    </div>
  );
}

function ProductSheet({ product }: { product: Product | null }) {
  if (!product) return null;
  return (
    <div className="product-sheet">
      <h3>商品信息</h3>
      <label>标准产品信息<textarea readOnly value={(product.profile_text || "").trim() || "还没有整理好。"} /></label>
      <label>标题<input readOnly value={product.title} /></label>
      <label>价格<input readOnly value={product.price} /></label>
      <label>卖点<textarea readOnly value={product.points} /></label>
      {(product.specs || []).filter((item) => item.label !== "中文品名" && item.label !== "核心卖点").map((item) => (
        <section key={item.label} className="spec-block">
          <p className="asset-kicker">{item.label}</p>
          <p className="spec-body">{item.value}</p>
        </section>
      ))}
      {product.page_text ? <label>页面原文<textarea readOnly value={product.page_text} /></label> : null}
    </div>
  );
}

function FactsForm({ product, onChange, onSubmit }: { product: Product | null; onChange: (product: Product) => void; onSubmit: (event: FormEvent) => void }) {
  if (!product) return null;
  return (
    <form onSubmit={onSubmit}>
      <h3>产品信息约束</h3>
      <p className="desc">只写页面上有的。抓取完成后，标准产品信息会显示在这一格。口播里的数字必须能在这里对上。</p>
      {product.profile_text ? <label>标准产品信息<textarea readOnly value={product.profile_text} /></label> : null}
      <label>标题<input value={product.title} onChange={(event) => onChange({ ...product, title: event.target.value })} /></label>
      <label>价格<input value={product.price} onChange={(event) => onChange({ ...product, price: event.target.value })} /></label>
      <label>卖点<textarea value={product.points} onChange={(event) => onChange({ ...product, points: event.target.value })} placeholder="只写页面上有的" /></label>
      {product.page_text ? <label>页面原文<textarea readOnly value={product.page_text} /></label> : null}
      <div className="actions"><button className="primary" type="submit">保存约束</button></div>
    </form>
  );
}

function ScenePicker({ scenes, used, onPick, onCreated, onError }: { scenes: Scene[]; used: Set<string>; onPick: (id: string) => void; onCreated: (id: string) => Promise<void>; onError: (text: string) => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!file) return onError("场景需要图片");
    const body = new FormData();
    body.set("name", name);
    body.set("description", description);
    body.set("image", file);
    try {
      const created = await request("/scenes", { method: "POST", body });
      setName("");
      setDescription("");
      setFile(null);
      onError("");
      await onCreated(created.id);
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有保存");
    }
  }

  return (
    <div>
      <h3>选择已有场景</h3>
      <div className="asset-grid">
        {scenes.length ? scenes.map((scene) => (
          <button type="button" key={scene.id} className={used.has(scene.id) ? "asset-card current" : "asset-card"} onClick={() => onPick(scene.id)}>
            <img src={scene.image} alt="" />
            <strong>{scene.name}</strong>
            <span>{used.has(scene.id) ? "再做一版" : "用这个"}</span>
          </button>
        )) : <p className="menu-empty">还没有场景，在下面新建。</p>}
      </div>
      <h3>新建场景</h3>
      <form onSubmit={create}>
        <label>名称<input value={name} onChange={(event) => setName(event.target.value)} required /></label>
        <label>描述词<textarea value={description} onChange={(event) => setDescription(event.target.value)} required placeholder="这张图取什么，不取什么" /></label>
        <label>图片<input type="file" accept="image/*" onChange={(event) => setFile(event.target.files?.[0] ?? null)} required /></label>
        <div className="actions"><button className="primary" type="submit">新建并放上画布</button></div>
      </form>
    </div>
  );
}

function ModelPicker({ people, used, onPick, onCreate, onClear }: { people: Person[]; used: Set<string>; onPick: (id: string) => void; onCreate: (event: FormEvent) => void; onClear: () => void }) {
  return (
    <div>
      <h3>选择模特</h3>
      <p className="desc">场景已经放上画布。这一步可选，只留一个模特。</p>
      <div className="asset-grid">
        {people.length ? people.map((person) => (
          <button type="button" key={person.id} className={used.has(person.id) ? "asset-card current" : "asset-card"} onClick={() => onPick(person.id)}>
            <img src={person.image} alt="" />
            <strong>{person.name}</strong>
            <span>{used.has(person.id) ? "当前模特" : "用这个"}</span>
          </button>
        )) : <p className="menu-empty">还没有模特。可以不选，或在下面新加。</p>}
      </div>
      {used.size ? <div className="actions"><button className="ghost" type="button" onClick={onClear}>不用模特</button></div> : null}
      <h3>新建模特</h3>
      <form onSubmit={onCreate}>
        <label>名字<input name="name" required /></label>
        <label>照片<input name="image" type="file" accept="image/*" required /></label>
        <div className="actions"><button className="primary" type="submit">新建并放上画布</button></div>
      </form>
    </div>
  );
}

function ViralPicker({ hits, currentId, onPick }: { hits: HitItem[]; currentId: string; onPick: (id: string) => void }) {
  const [openId, setOpenId] = useState("");
  const open = hits.find((hit) => hit.id === openId) ?? null;
  return (
    <div className="viral-panel">
      <h3>爆款复刻</h3>
      <p className="desc">点一条视频即选中，外框标出当前这条。分镜文字点开看全文。</p>
      <div className="viral-list">
        {hits.length ? hits.map((hit) => {
          const shots = (hit.shot_prompt || "").trim();
          return (
            <article key={hit.id} className={hit.id === currentId ? "viral-row current" : "viral-row"} onClick={() => { if (hit.id !== currentId) onPick(hit.id); }}>
              {hit.file ? <video src={hit.file} controls /> : <p className="desc">这条没有视频文件。</p>}
              <button className="shot-line" type="button" onClick={(event) => { event.stopPropagation(); setOpenId(hit.id); }}>{shots || "分镜还在整理"}</button>
            </article>
          );
        }) : <p className="menu-empty">这个商品还没有爆款视频。</p>}
      </div>
      {open ? (
        <div className="shot-dialog" role="dialog" onClick={() => setOpenId("")}>
          <div className="shot-card" onClick={(event) => event.stopPropagation()}>
            <strong>{open.name}</strong>
            <p>{(open.shot_prompt || "").trim() || "分镜还在整理"}</p>
            <div className="actions"><button className="ghost" type="button" onClick={() => setOpenId("")}>关闭</button></div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ReferenceBox({ onAdd }: { onAdd: (url: string) => Promise<void> }) {
  const [url, setUrl] = useState("");
  return (
    <form onSubmit={(event) => { event.preventDefault(); onAdd(url).then(() => setUrl("")); }}>
      <label>参考视频链接<input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="只取镜头和动作" /></label>
      <div className="actions"><button className="ghost" type="submit">加入参考</button></div>
    </form>
  );
}

function VariantEditor({ bundle, templates, onChange, onBoard, onTemplate, onTemplates, onError, onBack }: { bundle: VariantBundle; templates: TemplateItem[]; onChange: (bundle: VariantBundle) => void; onBoard: () => Promise<void>; onTemplate: (templateId: string) => Promise<void>; onTemplates: () => Promise<void>; onError: (text: string) => void; onBack: () => void }) {
  const variant = bundle.variant;
  const promptRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLDivElement>(null);
  const seenStatus = useRef(variant.status);
  const [busy, setBusy] = useState("");
  const [saved, setSaved] = useState("");
  const [filmNote, setFilmNote] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [videoStamp, setVideoStamp] = useState(0);
  const writing = busy === "正在生成中";
  const filming = busy === "正在提交视频" || variant.status === "queued" || variant.status === "running";
  const locked = writing || filming;
  const [steer, setSteer] = useState(variant.steer || "");
  const [noVoice, setNoVoice] = useState(variant.voiceover === 0);
  const [seconds, setSeconds] = useState(variant.duration_sec || 15);
  const [draftName, setDraftName] = useState("");
  const [draftBody, setDraftBody] = useState("");
  const [editingId, setEditingId] = useState("");
  const selectedTemplate = templates.find((item) => item.id === (variant.template_id || "")) || templates.find((item) => item.builtin) || templates[0];
  const ready = variant.status === "passed" && Boolean(variant.prompt_text);
  const canGenerate = Boolean(variant.prompt_text) && !locked && (variant.status === "passed" || variant.status === "hold" || variant.status === "failed" || variant.status === "returned");
  const manualHold = variant.qa_note === "与场景或分镜不一致，留到下一晚";
  useEffect(() => {
    setSteer(variant.steer || "");
    setNoVoice(variant.voiceover === 0);
    setSeconds(variant.duration_sec || 15);
  }, [variant.id, variant.steer, variant.voiceover, variant.duration_sec]);
  useEffect(() => {
    promptRef.current?.scrollIntoView({ block: "nearest" });
  }, [variant.id, variant.prompt_text]);
  useEffect(() => {
    if (!filming) return;
    setElapsed(0);
    const timer = setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [filming, variant.id]);
  useEffect(() => {
    const previous = seenStatus.current;
    seenStatus.current = variant.status;
    if ((variant.status === "queued" || variant.status === "running") && previous !== variant.status) setFilmNote("");
    if (variant.status === "returned" && previous !== "returned" && variant.video) {
      setVideoStamp(Date.now());
      setFilmNote("视频已生成，可以在下面播放。");
      videoRef.current?.scrollIntoView({ block: "center" });
    }
  }, [variant.status, variant.video]);
  async function act(path: string, options?: RequestInit) {
    const writingPrompt = path.endsWith("/check");
    if (writingPrompt) setSaved("");
    setBusy(writingPrompt ? "正在生成中" : "");
    try {
      onChange(await request(path, options));
      await onBoard();
      if (writingPrompt) {
        setSaved("提示词已生成，并保存到提示词库。");
        promptRef.current?.scrollIntoView({ block: "start" });
      }
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有完成");
    } finally {
      setBusy("");
    }
  }
  async function rewrite() {
    setSaved("");
    setBusy("正在生成中");
    try {
      await request(`/variants/${variant.id}/steer`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ steer, voiceover: !noVoice }),
      });
      onChange(await request(`/variants/${variant.id}/check`, { method: "POST" }));
      await onBoard();
      setSaved("提示词已生成，并保存到提示词库。");
      promptRef.current?.scrollIntoView({ block: "start" });
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有完成");
    } finally {
      setBusy("");
    }
  }
  async function generateVideo() {
    setFilmNote("");
    setSaved("");
    setBusy("正在提交视频");
    try {
      onChange(await request(`/variants/${variant.id}/generate`, { method: "POST" }));
      await onBoard();
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有生成视频");
    } finally {
      setBusy("");
    }
  }
  function addPreset(text: string) {
    setSteer((current) => current.includes(text) ? current : `${current.trim()}\n${text}`.trim());
  }
  const submitted = variant.status === "queued" || variant.status === "running";
  return (
    <div className={locked ? "variant-panel locked" : "variant-panel"}>
      {writing ? <div className="variant-lock" role="status">正在生成提示词</div> : null}
      {filming ? (
        <div className="variant-lock filming" role="status">
          <strong>{busy === "正在提交视频" ? "正在提交 Gemini Omni" : "正在生成视频"}</strong>
          <p>{variant.qa_note || "已交给 Gemini Omni。固定 10 秒、720p，通常要几分钟。"}</p>
          <p className="film-wait">已等待 {elapsed} 秒</p>
          <ol className="film-steps">
            <li className={submitted ? "done" : "now"}>提交提示词</li>
            <li className={submitted ? "now" : ""}>Gemini Omni 生成</li>
            <li>成片出现</li>
          </ol>
          <div className="film-bar" aria-hidden="true"><span /></div>
        </div>
      ) : null}
      {saved ? <p className="success-banner" role="status">{saved}</p> : null}
      {filmNote ? <p className="success-banner" role="status">{filmNote}</p> : null}
      {!filming && (variant.status === "failed" || (variant.status === "hold" && variant.qa_note && !manualHold)) ? <p className="fail-banner" role="alert">视频没有生成。{variant.qa_note}</p> : null}
      <div className="actions"><button className="ghost" type="button" onClick={onBack} disabled={locked}>返回商品资料</button></div>
      <h3>{filming ? "正在生成视频" : variant.status === "failed" ? "生成失败" : statusText(variant.status)} · 第 {variant.round} 轮</h3>
      <p className="desc">先在商品卡上传白底图，再点「检查并写提示词」。写提示词时，商品外观只提交白底图。没有白底图时，这一步不会开始。</p>
      <label>模板
        <select value={selectedTemplate?.id || ""} onChange={(event) => onTemplate(event.target.value).catch((error: Error) => onError(error.message))}>
          {templates.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </label>
      <div className="actions">
        <button className="ghost" type="button" onClick={() => { setEditingId(selectedTemplate?.id || "new"); setDraftName(selectedTemplate?.name || ""); setDraftBody(selectedTemplate?.body || ""); }}>编辑模板</button>
        <button className="ghost" type="button" onClick={() => { setEditingId("new"); setDraftName(""); setDraftBody(selectedTemplate?.body || ""); }}>新建模板</button>
      </div>
      {editingId ? (
        <form onSubmit={(event) => {
          event.preventDefault();
          const creating = editingId === "new";
          request(creating ? "/templates" : `/templates/${editingId}`, {
            method: creating ? "POST" : "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: draftName, body: draftBody }),
          }).then(async (saved: TemplateItem) => {
            await onTemplates();
            if (creating) await onTemplate(saved.id);
            setEditingId("");
          }).catch((error: Error) => onError(error.message));
        }}>
          <label>模板名称<input value={draftName} onChange={(event) => setDraftName(event.target.value)} required /></label>
          <label>模板正文<textarea value={draftBody} onChange={(event) => setDraftBody(event.target.value)} required /></label>
          <div className="actions">
            <button className="primary" type="submit">{editingId === "new" ? "存为新模板" : "保存模板"}</button>
            {editingId !== "new" && !templates.find((item) => item.id === editingId)?.builtin ? <button className="ghost" type="button" onClick={() => request(`/templates/${editingId}`, { method: "DELETE" }).then(async () => { await onTemplates(); const fallback = templates.find((item) => item.builtin); if (fallback) await onTemplate(fallback.id); setEditingId(""); }).catch((error: Error) => onError(error.message))}>删除</button> : null}
          </div>
        </form>
      ) : null}
      <label>视频时长
        <select value={seconds} onChange={(event) => {
          const next = Number(event.target.value);
          setSeconds(next);
          request(`/variants/${variant.id}/duration`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ duration_sec: next }) }).then(onChange).catch((error: Error) => onError(error.message));
        }}>
          {[10, 15, 30, 45, 60].map((item) => <option key={item} value={item}>{item} 秒</option>)}
        </select>
      </label>
      <div className="actions">
        <button className={ready ? "ghost" : "primary"} type="button" disabled={locked} onClick={() => act(`/variants/${variant.id}/check`, { method: "POST" })}>{writing ? "正在生成提示词" : ready ? "再检查一次" : "检查并写提示词"}</button>
        <button className="primary film-go" type="button" disabled={!canGenerate} aria-busy={filming} onClick={() => generateVideo()}>{filming ? "正在生成视频" : variant.status === "hold" || variant.status === "failed" || variant.status === "returned" ? "再生成一次" : "生成视频"}</button>
      </div>
      <p className="desc">检查会把这个秒数写进提示词。生成视频用 Gemini Omni 1.1，固定 10 秒、竖屏 9:16、720p。参考图只传两张白底图：一张完整商品白底图，一张多角度和产品细节白底图。其他商品详情图和场景图不会提交。15 秒的提示词会压成 10 秒再提交。已经出过片的版本可以再生成一次。</p>
      <div className="prompt-result" ref={promptRef}>
        <h3>生成的提示词</h3>
        {variant.prompt_text ? (
          <>
            <p className="desc">英文提示词用来生成视频。</p>
            <textarea readOnly value={variant.prompt_text} />
            {variant.prompt_zh ? (
              <>
                <h3>中文</h3>
                <textarea readOnly value={variant.prompt_zh} />
              </>
            ) : null}
          </>
        ) : <p className="desc">还没有提示词。点上面的「检查并写提示词」，写完后出现在这里。</p>}
      </div>
      <div className="prompt-steer">
        <h3>优化要求</h3>
        <p className="desc">勾选或写下要求，再点「按这些要求重写」。场景描述仍会原样保留。这里不能补充页面上没有的尺寸、功率和功效。</p>
        <label className="check-line"><input type="checkbox" checked={noVoice} onChange={(event) => setNoVoice(event.target.checked)} /> 不要口播</label>
        <div className="preset-row">
          {STEER_PRESETS.map(([label, text]) => <button key={label} className="ghost" type="button" onClick={() => addPreset(text)}>{label}</button>)}
        </div>
        <textarea value={steer} onChange={(event) => setSteer(event.target.value)} placeholder="例如：加强约束，商品只按产品图；手不要挡住商品。" />
        <div className="actions"><button className="primary" type="button" disabled={locked} onClick={() => rewrite().catch((error: Error) => onError(error.message))}>{writing ? "正在生成提示词" : "按这些要求重写"}</button></div>
      </div>
      <div className="actions">
        <button className="ghost" type="button" disabled={locked} onClick={() => act(`/variants/${variant.id}/adopt`, { method: "POST" })}>采用这一格</button>
        <button className="ghost" type="button" disabled={locked} onClick={() => act(`/variants/${variant.id}/hold`, { method: "POST" })}>下一晚再生成</button>
      </div>
      {(variant.check || []).length ? <h3>审查</h3> : null}
      <ul className="check-list">
        {(variant.check || []).map((item) => <li key={item.name} className={item.ok ? "check-ok" : "check-bad"}>{item.ok ? "通过" : "未通过"} · {item.name}{item.ok || !item.detail ? "" : `：${item.detail}`}</li>)}
      </ul>
      {!filming && manualHold ? <p className="desc">{variant.qa_note}</p> : null}
      {!filming && variant.qa_note && variant.status !== "hold" ? <p className="desc">{variant.qa_note}</p> : null}
      <div className={variant.video ? "film-stage ready" : "film-stage"} ref={videoRef}>
        <h3>成片</h3>
        {variant.video ? <video src={videoStamp ? `${variant.video}?t=${videoStamp}` : variant.video} controls /> : <p className="desc">{filming ? "生成完成后，成片会直接出现在这里。" : "点「生成视频」后，成片会出现在这里。"}</p>}
      </div>
    </div>
  );
}

function PeopleLibrary({ onError, message }: { onError: (text: string) => void; message: string }) {
  const [people, setPeople] = useState<Person[]>([]);
  const [personId, setPersonId] = useState("");

  async function reload() {
    const next = await request("/people?include_inactive=1");
    setPeople(next);
    return next as Person[];
  }

  useEffect(() => {
    reload().then((next) => setPersonId((current) => current || next[0]?.id || "new")).catch((error: Error) => onError(error.message));
  }, []);

  const selected = people.find((person) => person.id === personId) ?? null;

  return (
    <>
      <aside className="assets">
        <div className="asset-head"><strong>人物素材库</strong><button type="button" onClick={() => { setPersonId("new"); onError(""); }}>新增</button></div>
        <div className="row-list compact">
          {people.length ? people.map((person) => (
            <button type="button" key={person.id} className={person.id === personId ? "product-row current" : "product-row"} onClick={() => { setPersonId(person.id); onError(""); }}>
              <img src={person.image} alt="" />
              <strong>{person.name}</strong>
            </button>
          )) : <p className="menu-empty">还没有人物。点新增，放一张照片。</p>}
        </div>
      </aside>
      {personId === "new" || !selected ? (
        <PersonCreate reload={reload} onCreated={setPersonId} onError={onError} message={message} />
      ) : (
        <PersonDetail person={selected} reload={reload} onError={onError} message={message} />
      )}
    </>
  );
}

function PersonCreate({ reload, onCreated, onError, message }: { reload: () => Promise<Person[]>; onCreated: (id: string) => void; onError: (text: string) => void; message: string }) {
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState("");

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!file) return onError("人物需要图片");
    const body = new FormData();
    body.set("name", name);
    body.set("note", note);
    body.set("image", file);
    try {
      const created = await request("/people", { method: "POST", body });
      onError("");
      await reload();
      onCreated(created.id);
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有保存");
    }
  }

  return (
    <>
      <section className="viewer">
        <div className="viewer-label">预览</div>
        <div className="viewer-stage">{preview ? <img src={preview} alt="" /> : <div className="empty">人物照片会显示在这里</div>}</div>
      </section>
      <aside className="inspector">
        <h3>新增人物</h3>
        <p className="desc">模特照片进人物库。画布上可以选这个人，也可以不选。</p>
        {message ? <p className="notice">{message}</p> : null}
        <form onSubmit={create}>
          <label>名字<input value={name} onChange={(event) => setName(event.target.value)} required /></label>
          <label>备注<textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder="可留空" /></label>
          <label>照片<input type="file" accept="image/*" onChange={(event) => { const next = event.target.files?.[0] ?? null; setFile(next); setPreview(next ? URL.createObjectURL(next) : ""); }} required /></label>
          <div className="actions"><button className="primary" type="submit">保存</button></div>
        </form>
      </aside>
    </>
  );
}

function PersonDetail({ person, reload, onError, message }: { person: Person; reload: () => Promise<Person[]>; onError: (text: string) => void; message: string }) {
  const [name, setName] = useState(person.name);
  const [note, setNote] = useState(person.note);
  const [file, setFile] = useState<File | null>(null);
  const [shown, setShown] = useState(person.image);
  const photos = person.images?.length ? person.images : [person.image];

  useEffect(() => {
    setName(person.name);
    setNote(person.note);
    setFile(null);
    setShown(person.image);
  }, [person.id, person.name, person.note, person.image]);

  async function save(event: FormEvent) {
    event.preventDefault();
    const body = new FormData();
    body.set("name", name);
    body.set("note", note);
    body.set("active", person.active === false ? "0" : "1");
    if (file) body.set("image", file);
    try {
      await request(`/people/${person.id}`, { method: "PATCH", body });
      onError("");
      await reload();
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有保存");
    }
  }

  async function toggle() {
    const body = new FormData();
    body.set("active", person.active === false ? "1" : "0");
    await request(`/people/${person.id}`, { method: "PATCH", body });
    await reload();
  }

  return (
    <>
      <section className="viewer">
        <div className="viewer-label">{person.name} · {person.active === false ? "已停用" : "使用中"}</div>
        <div className="viewer-stage"><img src={shown} alt="" /></div>
        {photos.length > 1 ? (
          <div className="ref-strip">
            {photos.map((src) => (
              <button type="button" key={src} className={src === shown ? "current" : ""} onClick={() => setShown(src)}>
                <img src={src} alt="" />
              </button>
            ))}
          </div>
        ) : null}
      </section>
      <aside className="inspector">
        <h3>人物</h3>
        {message ? <p className="notice">{message}</p> : null}
        <form onSubmit={save}>
          <label>名字<input value={name} onChange={(event) => setName(event.target.value)} required /></label>
          <label>备注<textarea value={note} onChange={(event) => setNote(event.target.value)} /></label>
          <label>换照片<input type="file" accept="image/*" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
          <div className="actions">
            <button className="primary" type="submit">保存修改</button>
            <button className="ghost" type="button" onClick={() => toggle().catch((error: Error) => onError(error.message))}>{person.active === false ? "启用" : "停用"}</button>
          </div>
        </form>
      </aside>
    </>
  );
}

function JobStrip({ jobs, onDismiss }: { jobs: Job[]; onDismiss: (id: string) => void }) {
  if (!jobs.length) return null;
  return (
    <div className="job-strip" role="status">
      {jobs.map((job) => (
        <div key={job.id} className={`job-row ${job.status}`}>
          <strong>{job.label}</strong>
          <span>{JOB_STAGE[job.status] || job.status}</span>
          {job.error ? <em title={job.error}>{job.error}</em> : null}
          {job.status === "done" || job.status === "failed" ? <button type="button" className="ghost" onClick={() => onDismiss(job.id)}>关掉</button> : null}
        </div>
      ))}
    </div>
  );
}

function ProductRail({ products, jobs = [], productId, onSelect, onCreate, onDelete, exportable, onNotice }: { products: Product[]; jobs?: Job[]; productId: string; onSelect: (id: string) => void; onCreate?: () => void; onDelete?: () => void; exportable?: boolean; onNotice?: (text: string) => void }) {
  const selected = Boolean(productId && productId !== "new");
  const [picked, setPicked] = useState<string[]>([]);
  const [exporting, setExporting] = useState(false);
  const ids = products.map((product) => product.id);
  const selectedIds = picked.filter((id) => ids.includes(id));
  const allOn = ids.length > 0 && selectedIds.length === ids.length;

  function toggle(id: string) {
    setPicked((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  }

  async function exportMaterials() {
    if (!selectedIds.length || exporting) return;
    setExporting(true);
    try {
      const response = await fetch("/products/export-materials", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: selectedIds }),
      });
      if (!response.ok) {
        const text = await response.text();
        let detail = "";
        try {
          const body = JSON.parse(text) as { detail?: unknown };
          detail = typeof body.detail === "string" ? body.detail : "";
        } catch {
          detail = text.replace(/\s+/g, " ").slice(0, 180);
        }
        throw new Error(detail || "导出没有成功");
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "TikTok商品素材.zip";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1500);
      onNotice?.(`已开始下载 ${selectedIds.length} 个商品的素材压缩包`);
    } catch (error) {
      onNotice?.(error instanceof Error ? error.message : "导出没有成功");
    } finally {
      setExporting(false);
    }
  }

  return (
    <aside className="assets rail">
      <div className="asset-head">
        <strong>商品</strong>
        {exportable ? null : (
          <span className="head-actions">
            {onDelete ? <button className="danger" type="button" disabled={!selected} onClick={onDelete}>删除</button> : null}
            {onCreate ? <button type="button" onClick={onCreate}>新增</button> : null}
          </span>
        )}
      </div>
      {exportable ? (
        <div className="export-bar">
          <label><input type="checkbox" checked={allOn} onChange={() => setPicked(allOn ? [] : ids)} /> 全选</label>
          <button type="button" disabled={!selectedIds.length || exporting} onClick={() => exportMaterials()}>{exporting ? "导出中" : `导出${selectedIds.length ? ` ${selectedIds.length}` : ""}`}</button>
          {onDelete ? <button className="danger" type="button" disabled={!selected} onClick={onDelete}>删除</button> : null}
          {onCreate ? <button type="button" onClick={onCreate}>新增</button> : null}
        </div>
      ) : null}
      <div className="row-list compact">
        {products.length ? products.map((product) => (
          exportable ? (
            <div key={product.id} className={product.id === productId ? "product-row with-pick current" : "product-row with-pick"}>
              <input type="checkbox" aria-label={`勾选 ${product.title || "未命名商品"}`} checked={selectedIds.includes(product.id)} onChange={() => toggle(product.id)} />
              <button type="button" className="product-open" onClick={() => onSelect(product.id)}>
                {product.cover ? <img src={product.cover} alt="" /> : <span className="cover-empty">无图</span>}
                <span className="product-copy">
                  <strong>{product.title || "未命名商品"}</strong>
                  {jobMark(jobs.filter((job) => job.product_id === product.id)) ? <small className="job-mark">{jobMark(jobs.filter((job) => job.product_id === product.id))}</small> : null}
                </span>
              </button>
            </div>
          ) : (
            <button type="button" key={product.id} className={product.id === productId ? "product-row current" : "product-row"} onClick={() => onSelect(product.id)}>
              {product.cover ? <img src={product.cover} alt="" /> : <span className="cover-empty">无图</span>}
              <span className="product-copy">
                <strong>{product.title || "未命名商品"}</strong>
                {jobMark(jobs.filter((job) => job.product_id === product.id)) ? <small className="job-mark">{jobMark(jobs.filter((job) => job.product_id === product.id))}</small> : null}
              </span>
            </button>
          )
        )) : <p className="menu-empty">还没有商品。</p>}
      </div>
    </aside>
  );
}

function ProductSelect({ products, value, onChange }: { products: Product[]; value: string; onChange: (id: string) => void }) {
  return (
    <label>对应商品
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">不对应</option>
        {products.map((product) => <option key={product.id} value={product.id}>{product.title || "未命名商品"}</option>)}
      </select>
    </label>
  );
}

function PromptLibrary({ products, productId, onSelect, onError, message }: { products: Product[]; productId: string; onSelect: (id: string) => void; onError: (text: string) => void; message: string }) {
  const [items, setItems] = useState<PromptItem[]>([]);
  const [current, setCurrent] = useState("");

  async function reload() {
    const next = await request("/prompts");
    setItems(next);
    return next as PromptItem[];
  }

  useEffect(() => {
    reload().catch((error: Error) => onError(error.message));
  }, []);

  const mine = items.filter((item) => item.product_id === productId);
  useEffect(() => {
    setCurrent((id) => (mine.some((item) => item.id === id) ? id : mine[0]?.id || "new"));
  }, [productId, items]);

  const selected = mine.find((item) => item.id === current) ?? null;
  const columnTitle = selected?.scene_name ? `提示词 · ${selected.scene_name}` : "提示词";

  return (
    <>
      <ProductRail products={products} productId={productId} onSelect={onSelect} />
      <aside className="assets">
        <div className="asset-head"><strong>{columnTitle}</strong><button type="button" onClick={() => { setCurrent("new"); onError(""); }}>新增</button></div>
        <div className="row-list compact">
          {mine.length ? mine.map((item) => (
            <button type="button" key={item.id} className={item.id === current ? "product-row current" : "product-row"} onClick={() => { setCurrent(item.id); onError(""); }}>
              <span className="cover-empty">{item.tag || "词"}</span>
              <span>
                <strong>{item.scene_name ? `${item.name} · ${item.scene_name}` : item.name}</strong>
                <small>{promptWhen(item.created_at)}</small>
              </span>
            </button>
          )) : <p className="menu-empty">{productId ? "这个商品还没有提示词。" : "先在左边点一个商品。"}</p>}
        </div>
      </aside>
      <PromptEditor item={current === "new" ? null : selected} products={products} lockedProductId={productId} message={message} onError={onError} onSaved={async (id) => { await reload(); setCurrent(id); }} onDeleted={async () => { const next = await reload(); const left = (next as PromptItem[]).filter((item) => item.product_id === productId); setCurrent(left[0]?.id || "new"); }} />
    </>
  );
}

function promptWhen(value: string | undefined) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 16).replace("T", " ");
  return date.toLocaleString("zh-CN", { hour12: false });
}

function PromptEditor({ item, products, lockedProductId, message, onError, onSaved, onDeleted }: { item: PromptItem | null; products: Product[]; lockedProductId: string; message: string; onError: (text: string) => void; onSaved: (id: string) => Promise<void>; onDeleted: () => Promise<void> }) {
  const [name, setName] = useState("");
  const [body, setBody] = useState("");
  const [bodyZh, setBodyZh] = useState("");
  const [tag, setTag] = useState("");
  const [productId, setProductId] = useState("");

  useEffect(() => {
    setName(item?.name || "");
    setBody(item?.body || "");
    setBodyZh(item?.body_zh || "");
    setTag(item?.tag || "");
    setProductId(item?.product_id || lockedProductId || "");
  }, [item?.id, item?.name, item?.body, item?.body_zh, item?.tag, item?.product_id, lockedProductId]);

  async function save(event: FormEvent) {
    event.preventDefault();
    const payload = { name, body, body_zh: bodyZh, tag, product_id: productId };
    try {
      const saved = await request(item ? `/prompts/${item.id}` : "/prompts", {
        method: item ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      onError("");
      await onSaved(saved.id);
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有保存");
    }
  }

  async function remove() {
    if (!item) return;
    await request(`/prompts/${item.id}`, { method: "DELETE" });
    await onDeleted();
  }

  return (
    <section className="viewer">
      <div className="prompt-sheet">
      <h3>{item?.scene_name ? `提示词 · ${item.scene_name}` : item ? "提示词" : "新增提示词"}</h3>
      <p className="desc">左边点商品，这里是它的提示词。标题里的场景是生成这条提示词时用的场景。</p>
      {message ? <p className="notice">{message}</p> : null}
      <form onSubmit={(event) => save(event).catch((error: Error) => onError(error.message))}>
        <label>名称<input value={name} onChange={(event) => setName(event.target.value)} required /></label>
        <label>标签
          <input value={tag} onChange={(event) => setTag(event.target.value)} placeholder="优质、低质、需要修改" />
        </label>
        <div className="preset-row">
          {PROMPT_TAGS.map((label) => <button key={label} className={tag === label ? "primary" : "ghost"} type="button" onClick={() => setTag(tag === label ? "" : label)}>{label}</button>)}
        </div>
        <label>英文提示词<textarea value={body} onChange={(event) => setBody(event.target.value)} required placeholder="写给视频生成的提示词" /></label>
        <label>中文<textarea value={bodyZh} onChange={(event) => setBodyZh(event.target.value)} placeholder="同一条提示词的中文" /></label>
        <ProductSelect products={products} value={productId} onChange={setProductId} />
        <div className="actions">
          <button className="primary" type="submit">保存</button>
          {item ? <button className="ghost" type="button" onClick={() => remove().catch((error: Error) => onError(error.message))}>删除</button> : null}
        </div>
      </form>
      </div>
    </section>
  );
}

function speechTags(raw: string | undefined): string[] {
  if (!raw) return [];
  try {
    const data = JSON.parse(raw);
    return Array.isArray(data) ? data.filter((item) => typeof item === "string" && item.trim()) : [];
  } catch {
    return [];
  }
}

function speechRowClass(item: SpeechItem, current: string) {
  const mark = item.speech_ok === 1 ? "speech-pass" : item.speech_ok === 0 ? "speech-fail" : "";
  return ["product-row", item.id === current ? "current" : "", mark].filter(Boolean).join(" ");
}

function speechMarkClass(item: SpeechItem) {
  if (item.speech_ok === 1) return "cover-empty speech-pass";
  if (item.speech_ok === 0) return "cover-empty speech-fail";
  return "cover-empty";
}

function SpeechLibrary({ products, productId, onSelect, onError, message }: { products: Product[]; productId: string; onSelect: (id: string) => void; onError: (text: string) => void; message: string }) {
  const [items, setItems] = useState<SpeechItem[]>([]);
  const [current, setCurrent] = useState("");
  const [busy, setBusy] = useState("");
  const [tagDraft, setTagDraft] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  async function reload() {
    const next = await request("/speeches");
    setItems(next);
    return next as SpeechItem[];
  }

  useEffect(() => {
    reload().catch((error: Error) => onError(error.message));
  }, []);

  const mine = items.filter((item) => item.product_id === productId);
  useEffect(() => {
    setCurrent((id) => (mine.some((item) => item.id === id) ? id : mine[0]?.id || ""));
  }, [productId, items]);

  const selected = mine.find((item) => item.id === current) ?? null;

  async function upload(file: File) {
    if (!productId) {
      onError("先在左边点一个商品。");
      return;
    }
    const body = new FormData();
    body.set("product_id", productId);
    body.set("file", file);
    setBusy("upload");
    try {
      const saved = await request("/speeches", { method: "POST", body });
      await reload();
      setCurrent(saved.id);
      onError("");
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有上传");
    } finally {
      setBusy("");
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function extract() {
    if (!selected) return;
    setBusy("hear");
    try {
      const saved = await request(`/variants/${selected.id}/speech`, { method: "POST" });
      applySpeech(saved.variant);
      onError("");
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有完成");
    } finally {
      setBusy("");
    }
  }

  async function approve() {
    if (!selected) return;
    setBusy("pass");
    try {
      const saved = await request(`/variants/${selected.id}/speech/approve`, { method: "POST" });
      applySpeech(saved.variant);
      onError("");
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有完成");
    } finally {
      setBusy("");
    }
  }

  function applySpeech(variant: { speech_text?: string; speech_note?: string; speech_ok?: number | null; speech_tags?: string }) {
    const tags = speechTags(variant.speech_tags);
    setItems((rows) => rows.map((row) => row.id === selected?.id ? { ...row, speech_text: variant.speech_text || "", speech_note: variant.speech_note || "", speech_ok: variant.speech_ok ?? null, tags } : row));
  }

  async function reject() {
    if (!selected) return;
    setBusy("reject");
    try {
      const saved = await request(`/variants/${selected.id}/speech/reject`, { method: "POST" });
      applySpeech(saved.variant);
      onError("");
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有完成");
    } finally {
      setBusy("");
    }
  }

  async function saveTags(tags: string[]) {
    if (!selected) return;
    setBusy("tag");
    try {
      const saved = await request(`/variants/${selected.id}/speech/tags`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tags }),
      });
      applySpeech(saved.variant);
      setTagDraft("");
      onError("");
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有保存标签");
    } finally {
      setBusy("");
    }
  }

  async function removeFilm() {
    if (!selected) return;
    if (!window.confirm("删除这条成片？删掉后审核列表里就没有了。")) return;
    setBusy("delete");
    try {
      await request(`/speeches/${selected.id}`, { method: "DELETE" });
      await reload();
      onError("");
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有删除");
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <ProductRail products={products} productId={productId} onSelect={onSelect} />
      <aside className="assets">
        <div className="asset-head">
          <strong>口播审核</strong>
          <button type="button" disabled={Boolean(busy) || !productId} onClick={() => fileRef.current?.click()}>{busy === "upload" ? "正在上传" : "上传"}</button>
          <input ref={fileRef} className="file-picker" type="file" accept="video/mp4,video/webm,video/quicktime,.mp4,.mov,.webm,.m4v" hidden onChange={(event) => { const file = event.target.files?.[0]; if (file) upload(file).catch((error: Error) => onError(error.message)); }} />
        </div>
        <div className="row-list compact">
          {mine.length ? mine.map((item) => (
            <button type="button" key={item.id} className={speechRowClass(item, current)} onClick={() => { setCurrent(item.id); onError(""); }}>
              <span className={speechMarkClass(item)}>{item.speech_ok === 1 ? "过" : item.speech_ok === 0 ? "驳" : "待"}</span>
              <span>
                <strong>{item.scene_name || item.label || "成片"}</strong>
                {item.tags?.length ? <em>{item.tags.join(" · ")}</em> : null}
              </span>
            </button>
          )) : <p className="menu-empty">{productId ? "点上传，把成片放进来。" : "先在左边点一个商品。"}</p>}
        </div>
      </aside>
      <section className="viewer">
        <div className="viewer-label">{selected ? (selected.scene_name || selected.label || "成片") : "口播审核"}</div>
        <div className="viewer-stage">
          {selected?.video ? <video src={selected.video} controls /> : <div className="empty">上传一条成片后，会显示在这里。</div>}
        </div>
      </section>
      <aside className="inspector">
        <h3>口播审核</h3>
        <p className="desc">先提取成片里的口播，再译成中文。通过的标成绿色，不通过的标成红色。标签用来记下这条成片已经发给哪个账号。</p>
        {message ? <p className="notice">{message}</p> : null}
        {busy === "upload" ? <p className="notice">正在上传。</p> : null}
        {busy === "hear" ? <p className="notice">正在提取口播并翻译。</p> : null}
        {selected ? (
          <>
            <div className="actions">
              <button className="ghost" type="button" disabled={Boolean(busy)} onClick={() => extract().catch((error: Error) => onError(error.message))}>{busy === "hear" ? "正在提取" : "提取口播"}</button>
              <button className="primary" type="button" disabled={Boolean(busy) || !selected.speech_text || selected.speech_ok === 1} onClick={() => approve().catch((error: Error) => onError(error.message))}>{selected.speech_ok === 1 ? "口播正常" : "审核通过"}</button>
              <button className="danger" type="button" disabled={Boolean(busy) || selected.speech_ok === 0} onClick={() => reject().catch((error: Error) => onError(error.message))}>{selected.speech_ok === 0 ? "未通过" : "不通过"}</button>
              <button className="ghost" type="button" disabled={Boolean(busy)} onClick={() => removeFilm().catch((error: Error) => onError(error.message))}>{busy === "delete" ? "正在删除" : "删除视频"}</button>
            </div>
            {selected.speech_ok === 1 ? <p className="check-ok">口播正常</p> : null}
            {selected.speech_ok === 0 ? <p className="check-bad">审核未通过</p> : null}
            <label>发布标签
              <span className="tag-list">
                {(selected.tags || []).map((tag) => (
                  <button key={tag} type="button" disabled={Boolean(busy)} onClick={() => saveTags((selected.tags || []).filter((item) => item !== tag)).catch((error: Error) => onError(error.message))}>{tag} ×</button>
                ))}
              </span>
              <input value={tagDraft} placeholder="例如 tk2" onChange={(event) => setTagDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); const next = tagDraft.trim(); if (next) saveTags([...(selected.tags || []), next]).catch((error: Error) => onError(error.message)); } }} />
            </label>
            <div className="actions">
              <button className="ghost" type="button" disabled={Boolean(busy) || !tagDraft.trim()} onClick={() => saveTags([...(selected.tags || []), tagDraft.trim()]).catch((error: Error) => onError(error.message))}>添加标签</button>
            </div>
            {selected.speech_text ? <label>识别文字<textarea readOnly value={selected.speech_text} /></label> : <p className="desc">还没有识别文字。</p>}
            {selected.speech_text && selected.speech_note ? <label>中文<textarea readOnly value={selected.speech_note} /></label> : null}
            {!selected.speech_text && selected.speech_note ? <p className="desc">{selected.speech_note}</p> : null}
          </>
        ) : <p className="desc">点上传，选择一条成片。</p>}
      </aside>
    </>
  );
}

function HitLibrary({ products, productId, onSelect, onError, message }: { products: Product[]; productId: string; onSelect: (id: string) => void; onError: (text: string) => void; message: string }) {
  const [items, setItems] = useState<HitItem[]>([]);
  const [current, setCurrent] = useState("");

  async function reload() {
    const next = await request("/hits");
    setItems(next);
    return next as HitItem[];
  }

  useEffect(() => {
    reload().catch((error: Error) => onError(error.message));
  }, []);

  const mine = items.filter((item) => item.product_id === productId);
  useEffect(() => {
    setCurrent((id) => (mine.some((item) => item.id === id) ? id : mine[0]?.id || "new"));
  }, [productId, items]);

  const selected = mine.find((item) => item.id === current) ?? null;

  return (
    <>
      <ProductRail products={products} productId={productId} onSelect={onSelect} />
      <aside className="assets">
        <div className="asset-head"><strong>爆款素材</strong><button type="button" onClick={() => { setCurrent("new"); onError(""); }}>新增</button></div>
        <div className="row-list compact">
          {mine.length ? mine.map((item) => (
            <button type="button" key={item.id} className={item.id === current ? "product-row current" : "product-row"} onClick={() => { setCurrent(item.id); onError(""); }}>
              {item.kind === "image" && item.file ? <img src={item.file} alt="" /> : <span className="cover-empty">片</span>}
              <span><strong>{item.name}</strong></span>
            </button>
          )) : <p className="menu-empty">{productId ? "这个商品还没有爆款素材。" : "先在左边点一个商品。"}</p>}
        </div>
      </aside>
      <HitEditor item={current === "new" ? null : selected} products={products} lockedProductId={productId} message={message} onError={onError} onSaved={async (id) => { await reload(); setCurrent(id); }} onDeleted={async () => { const next = await reload(); const left = (next as HitItem[]).filter((item) => item.product_id === productId); setCurrent(left[0]?.id || "new"); }} />
    </>
  );
}

function HitEditor({ item, products, lockedProductId, message, onError, onSaved, onDeleted }: { item: HitItem | null; products: Product[]; lockedProductId: string; message: string; onError: (text: string) => void; onSaved: (id: string) => Promise<void>; onDeleted: () => Promise<void> }) {
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [productId, setProductId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [sourceUrl, setSourceUrl] = useState("");

  useEffect(() => {
    setName(item?.name || "");
    setNote(item?.note || "");
    setProductId(item?.product_id || lockedProductId || "");
    setFile(null);
    setSourceUrl("");
  }, [item?.id, item?.name, item?.note, item?.product_id, lockedProductId]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!item && !file && !sourceUrl.trim()) return onError("上传文件，或填写视频链接");
    const body = new FormData();
    body.set("name", name);
    body.set("note", note);
    body.set("product_id", productId);
    if (sourceUrl.trim()) body.set("source_url", sourceUrl.trim());
    if (file) body.set("file", file);
    try {
      const saved = await request(item ? `/hits/${item.id}` : "/hits", { method: item ? "PATCH" : "POST", body });
      onError("");
      await onSaved(saved.id);
    } catch (error) {
      onError(error instanceof Error ? error.message : "没有保存");
    }
  }

  async function remove() {
    if (!item) return;
    await request(`/hits/${item.id}`, { method: "DELETE" });
    await onDeleted();
  }

  return (
    <>
      <section className="viewer">
        <div className="viewer-label">{item ? item.name : "预览"}</div>
        <div className="viewer-stage">
          {item?.kind === "video" && item.file ? <video src={item.file} controls /> : item?.file ? <img src={item.file} alt="" /> : <div className="empty">图片或视频会显示在这里。只取镜头和动作，不取脸、声音和音乐。</div>}
        </div>
      </section>
      <aside className="inspector">
        <h3>{item ? "编辑爆款素材" : "新增爆款素材"}</h3>
        <p className="desc">左边选中的商品会接收这条素材。上传文件，或粘贴视频链接，保存后下载并挂到这个商品。</p>
        {message ? <p className="notice">{message}</p> : null}
        <form onSubmit={(event) => save(event).catch((error: Error) => onError(error.message))}>
          <label>名称<input value={name} onChange={(event) => setName(event.target.value)} required /></label>
          <label>取什么<textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder="镜头和动作，不取脸、声音、音乐" /></label>
          {item?.kind === "video" ? <label>分镜提示词<textarea readOnly value={item.shot_prompt || "视频保存后会整理景别、运镜和动作。"} /></label> : null}
          <label>{item ? "换文件" : "文件"}<input type="file" accept="image/*,video/mp4,video/webm,video/quicktime" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
          <label>视频链接<input value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} placeholder="粘贴 TikTok 视频链接，保存后下载" /></label>
          <ProductSelect products={products} value={productId} onChange={setProductId} />
          <div className="actions">
            <button className="primary" type="submit">保存</button>
            {item ? <button className="ghost" type="button" onClick={() => remove().catch((error: Error) => onError(error.message))}>删除</button> : null}
          </div>
        </form>
      </aside>
    </>
  );
}
