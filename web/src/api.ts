import type {
  MapUsageRow,
  AddressCandidate,
  CultureWork,
  ChangelogEntry,
  FoodDish,
  RouteCard,
  RouteDetail,
  DayTripHome,
  DayTripList,
  DayTripOrigin,
  Alternative,
  AreaRec,
  Attachment,
  AttachmentsData,
  AttractionRef,
  BudgetData,
  BudgetSettingsBody,
  CityInfo,
  DestinationJob,
  DestinationResolve,
  EditOp,
  EditResponse,
  IntakeGap,
  Itinerary,
  LlmUsage,
  NlIntent,
  PackingData,
  LookupHit,
  LookupResult,
  CommuteResult,
  ParseResult,
  PlaceDetail,
  PlaceDto,
  PlanCard,
  PlanDetail,
  PlanResponse,
  ReservationItem,
  ReservationSaved,
  RouteData,
  ShareOptions,
  ShareStatus,
  SharedPlan,
  Stay,
  Task,
  TaskCard,
  TaskTrace,
  ThemeDetail,
  ThemeInfo,
  TodosData,
  WeatherData,
} from "./types";

import { CACHE_HIT, FRESH } from "./offline";

const BASE = (import.meta.env.VITE_API_BASE ?? "http://localhost:8137").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
  /** 422：需求不完整时后端回传的追问结构。 */
  get intakeGap(): IntakeGap | null {
    const d = this.detail as { detail?: IntakeGap } | IntakeGap | undefined;
    if (d && typeof d === "object" && "questions" in d) return d as IntakeGap;
    if (d && typeof d === "object" && "detail" in d && d.detail && "questions" in d.detail) {
      return d.detail as IntakeGap;
    }
    return null;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (e) {
    window.dispatchEvent(new Event(CACHE_HIT)); // 请求发不出去：页面进入「离线 · 只读」
    throw new ApiError(0, `连不上后端 ${BASE}，确认 uvicorn 在跑`, e);
  }
  // Service Worker 断网时拿缓存顶上，会加 x-from-cache；页面据此提示「只读」
  window.dispatchEvent(new Event(res.headers.get("x-from-cache") ? CACHE_HIT : FRESH));
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const msg =
      (body && (body.detail?.message || body.detail || body.message)) || `HTTP ${res.status}`;
    throw new ApiError(res.status, typeof msg === "string" ? msg : `HTTP ${res.status}`, body);
  }
  return body as T;
}

/** 只读的 GET：任何失败都抛出，但不触发「离线 · 只读」（调用方自己决定怎么降级）。 */
async function quietGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`, null);
  return (await res.json()) as T;
}

/** 后端在 Content-Disposition 里给的文件名（filename*=UTF-8''… 或 filename="…"）；没有就是 null。 */
function serverFilename(res: Response): string | null {
  const cd = res.headers.get("content-disposition") ?? "";
  const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(cd);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim());
    } catch {
      /* 编码不对：看下一种写法 */
    }
  }
  return /filename\s*=\s*"([^"]+)"/i.exec(cd)?.[1] ?? null;
}

/** 先把文件取回来（生成可能要几秒），再触发浏览器下载；失败抛出 ApiError，不新开空白标签页。 */
async function downloadFile(url: string, filename: string): Promise<void> {
  let res: Response;
  try {
    res = await fetch(url);
  } catch (e) {
    throw new ApiError(0, `连不上后端 ${BASE}，确认 uvicorn 在跑`, e);
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      msg = (typeof body?.detail === "string" ? body.detail : body?.detail?.message) || msg;
    } catch {
      /* 不是 JSON：用状态码 */
    }
    throw new ApiError(res.status, msg, null);
  }
  const href = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = href;
  a.download = serverFilename(res) ?? filename; // 多城市的文件名要带上所有目的地：以后端给的为准
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 10000);
}

const WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"];

/** 把地点库的一个点转成地图 / 贴士组件用的形状。 */
/** 照片的完整地址（后端给的是相对路径）。 */
export function mediaSrc(path: string): string {
  return `${BASE}${path}`;
}

export function placeToAttraction(p: PlaceDto): AttractionRef {
  return {
    name_zh: p.name_zh,
    name_en: p.name_en,
    name_local: p.name_local ?? null,
    arr: 0,
    type: p.category,
    duration_min: p.duration_min,
    metro: p.metro ?? "",
    closed: p.closed_weekdays.length ? p.closed_weekdays.map((d) => `周${WEEKDAY_ZH[d]}`).join("、") : null,
    booking: p.booking.required,
    interests: p.interests,
    note: p.note,
    lat: p.lat,
    lng: p.lng,
    url: p.official_url,
    address: p.address,
    ticketing: p.ticketing,
    ticket_note: p.ticket_note,
    highlights: p.highlights,
    long_term_closed: p.long_term_closed,
    place_id: p.id,
    area: p.area,
    url_status: p.url_status,
    verified_at: p.verified_at,
    media: p.media ?? null,
  };
}

function homeBody(h: DayTripHome) {
  return { city: h.city, city_zh: h.city_zh, country: h.country, lat: h.lat, lng: h.lng };
}

export const api = {
  /** 周边一日游（规格 08 3.4）：出发地、目的地列表、「排一天」的需求。 */
  daytripOrigins(): Promise<DayTripOrigin[]> {
    return request("/daytrips/origins");
  },
  /** 住址 → 候选地址。住址只发给时刻表服务查坐标；这几个接口都用 POST，免得住址进访问日志。 */
  daytripGeocode(q: string): Promise<AddressCandidate[]> {
    return request("/daytrips/geocode", { method: "POST", body: JSON.stringify({ q }) });
  },
  daytripSearch(home: DayTripHome, date: string, depart: string): Promise<DayTripList> {
    return request("/daytrips/search", {
      method: "POST",
      body: JSON.stringify({ ...homeBody(home), date, depart, origin: home.preset ?? null }),
    });
  },
  daytripPlanRequest(
    home: DayTripHome,
    q: { destination: string; date: string; depart: string; back?: string; kind: "fastest" | "regional" },
  ): Promise<{ payload: Record<string, unknown>; arrive: string; leave: string; last_return: string; minutes: number }> {
    return request("/daytrips/plan-request", {
      method: "POST",
      body: JSON.stringify({ ...homeBody(home), ...q, back: q.back || null }),
    });
  },
  /** 自然语言填表（规格 M01 S10–S13）：无状态，后端不存正文；只读意图，不生成行程。 */
  intakeNl(text: string): Promise<NlIntent> {
    return request("/intake/nl", { method: "POST", body: JSON.stringify({ text }) });
  },
  /** 文件附件（规格 M10 S5）：请求体就是文件字节，文件名和挂靠放在查询参数里。 */
  attachments(id: string): Promise<AttachmentsData> {
    return request(`/plans/${encodeURIComponent(id)}/attachments`);
  },
  uploadAttachment(id: string, file: File, target?: { kind: string; ref: string | null }): Promise<Attachment> {
    const q = new URLSearchParams({ name: file.name });
    if (target && target.kind !== "plan") {
      q.set("target_kind", target.kind);
      if (target.ref) q.set("target_ref", target.ref);
    }
    return request(`/plans/${encodeURIComponent(id)}/attachments?${q}`, {
      method: "POST",
      body: file,
      headers: { "content-type": "application/octet-stream" },
    });
  },
  patchAttachment(id: string, aid: string, body: { target_kind?: string; target_ref?: string | null; restore?: boolean }): Promise<Attachment> {
    return request(`/plans/${encodeURIComponent(id)}/attachments/${aid}`, { method: "PATCH", body: JSON.stringify(body) });
  },
  deleteAttachment(id: string, aid: string, permanent = false): Promise<null> {
    return request(`/plans/${encodeURIComponent(id)}/attachments/${aid}${permanent ? "?permanent=1" : ""}`, { method: "DELETE" });
  },
  attachmentUrl(id: string, aid: string, download = false): string {
    return `${BASE}/plans/${encodeURIComponent(id)}/attachments/${aid}/file${download ? "?download=1" : ""}`;
  },
  /** 解析一篇笔记（规格 M12）：无状态，后端不存正文。 */
  /** 导入弹窗用：哪些来源配好了（只有「有没有」，没有 Cookie 本身）。 */
  materialSources(): Promise<{ xhs_cookie: boolean }> {
    return request("/materials/sources");
  },
  parseMaterial(input: { text: string } | { url: string }): Promise<ParseResult> {
    return request("/materials/parse", { method: "POST", body: JSON.stringify(input) });
  },
  /** 提交后台生成任务，立刻返回任务号；进度用 getTask 轮询。 */
  createTask(payload: Record<string, unknown>): Promise<{ task_id: string; status: string }> {
    return request("/tasks", { method: "POST", body: JSON.stringify(payload) });
  },
  /** 多城市行程里改换城市的交通：这两座城市按新时间重排（锁定的点不动），生成一个新版本。 */
  savePlanTransfer(
    planId: string,
    position: number,
    body: { mode: string; depart_at: string; arrive_at: string; number?: string },
  ): Promise<PlanResponse> {
    return request<PlanResponse>(`/plans/${encodeURIComponent(planId)}/transfers/${position}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  },
  /** 「正在生成」区：没生成完的任务（排队 / 进行中）加 24 小时内失败的。 */
  unfinishedTasks(): Promise<TaskCard[]> {
    return request<TaskCard[]>("/tasks?unfinished=true");
  },
  /** 移除一张失败的任务卡（进行中的不能移除）。 */
  removeTask(id: string): Promise<null> {
    return request<null>(`/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
  },
  getTask(id: string): Promise<Task> {
    return request<Task>(`/tasks/${encodeURIComponent(id)}`);
  },
  cities(): Promise<CityInfo[]> {
    return request<CityInfo[]>("/cities");
  },
  mapUsage(): Promise<{ services: MapUsageRow[] }> {
    return request("/map/usage");
  },
  mapConfig(): Promise<{ provider: string; reason?: string; js_key?: string; security_code?: string }> {
    return request("/map/config");
  },
  planCards(archived = false): Promise<PlanCard[]> {
    return request<PlanCard[]>(`/plan-cards${archived ? "?archived=true" : ""}`);
  },
  /** 整体平移日期：生成新版本，返回平移后新出现的闭馆日冲突。 */
  shiftDates(id: string, startDate: string): Promise<PlanResponse> {
    return request<PlanResponse>(`/plans/${encodeURIComponent(id)}/shift`, {
      method: "POST",
      body: JSON.stringify({ start_date: startDate }),
    });
  },
  duplicatePlan(id: string): Promise<PlanResponse> {
    return request<PlanResponse>(`/plans/${encodeURIComponent(id)}/duplicate`, { method: "POST" });
  },
  /** 永久删除（所有版本和分享链接一起）；调用前必须让用户确认。 */
  deletePlan(id: string): Promise<null> {
    return request<null>(`/plans/${encodeURIComponent(id)}`, { method: "DELETE" });
  },
  archivePlan(id: string, archived: boolean): Promise<{ archived: boolean }> {
    return request<{ archived: boolean }>(`/plans/${encodeURIComponent(id)}/archive`, {
      method: "PUT",
      body: JSON.stringify({ archived }),
    });
  },
  /** 文化入门（规格 09 十三）：某座城市的电影 / 纪录片 / 书，每部带你看哪些地方。 */
  culture(city = "paris"): Promise<CultureWork[]> {
    return request<CultureWork[]>(`/culture?city=${encodeURIComponent(city)}`);
  },
  /** 美食（规格 09 十七）：名菜名点，有招牌的带场所。 */
  changelog(): Promise<ChangelogEntry[]> {
    return request<ChangelogEntry[]>("/changelog");
  },

  food(city = "paris"): Promise<FoodDish[]> {
    return request<FoodDish[]>(`/food?city=${encodeURIComponent(city)}`);
  },
  /** 深度游学路线（规格 09）：某座城市的路线卡片和一条路线的详情。 */
  deepRoutes(city = "paris"): Promise<RouteCard[]> {
    return request<RouteCard[]>(`/routes?city=${encodeURIComponent(city)}`);
  },
  deepRoute(id: string, city = "paris"): Promise<RouteDetail> {
    return request<RouteDetail>(`/routes/${encodeURIComponent(id)}?city=${encodeURIComponent(city)}`);
  },
  themes(city?: string): Promise<ThemeInfo[]> {
    return request<ThemeInfo[]>(city ? `/themes?city=${encodeURIComponent(city)}` : "/themes");
  },
  resolveDestination(text: string): Promise<DestinationResolve> {
    return request<DestinationResolve>("/destinations/resolve", { method: "POST", body: JSON.stringify({ text }) });
  },
  buildDestination(qid: string, query: string): Promise<{ status: string; job_id?: string; city_id?: string; name_zh?: string }> {
    return request("/destinations/build", { method: "POST", body: JSON.stringify({ qid, query }) });
  },
  destinationJob(id: string): Promise<DestinationJob> {
    return request<DestinationJob>(`/destinations/jobs/${encodeURIComponent(id)}`);
  },
  theme(id: string, city?: string): Promise<ThemeDetail> {
    const q = city ? `?city=${encodeURIComponent(city)}` : "";
    return request<ThemeDetail>(`/themes/${encodeURIComponent(id)}${q}`);
  },
  placeDetail(id: string, city?: string): Promise<PlaceDetail> {
    const q = city ? `?city=${encodeURIComponent(city)}` : "";
    return request<PlaceDetail>(`/places/${encodeURIComponent(id)}${q}`);
  },
  /** 地点库内搜索（名字 / 英文名 / 别名 / 区域）。 */
  async searchPlaces(q: string, city?: string): Promise<AttractionRef[]> {
    const c = city ? `&city=${encodeURIComponent(city)}` : "";
    return (await request<PlaceDto[]>(`/places/search?q=${encodeURIComponent(q)}${c}`)).map(placeToAttraction);
  },
  /** 点了「在高德里搜」才调用（国内城市）：搜地点库里没有的地点。 */
  lookupPlace(q: string, city?: string): Promise<LookupResult> {
    const c = city ? `&city=${encodeURIComponent(city)}` : "";
    return request<LookupResult>(`/places/lookup?q=${encodeURIComponent(q)}${c}`);
  },
  /** 一天里相邻两点的真实通勤（高德）：点了才查，不缓存、不保存。 */
  commute(
    city: string,
    legs: { from_lat: number; from_lng: number; to_lat: number; to_lng: number }[],
    drive = false,
  ): Promise<CommuteResult> {
    return request<CommuteResult>("/commute", { method: "POST", body: JSON.stringify({ city, legs, drive }) });
  },
  /** 把高德里搜到的点加进这座城市的地点库（草稿档：没有营业时间和价格）。 */
  async addCustomPlace(city: string, h: LookupHit): Promise<AttractionRef> {
    const body = { city, name: h.name, address: h.address, area: h.area, category: h.category, lat: h.lat, lng: h.lng };
    return placeToAttraction(await request<PlaceDto>("/places/custom", { method: "POST", body: JSON.stringify(body) }));
  },
  async places(city?: string): Promise<AttractionRef[]> {
    const q = city ? `?city=${encodeURIComponent(city)}` : "";
    return (await request<PlaceDto[]>(`/places${q}`)).map(placeToAttraction);
  },
  createPlan(payload: Record<string, unknown>, repair = false): Promise<PlanResponse> {
    return request<PlanResponse>(`/plans?repair=${repair}`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  getPlan(id: string, version?: number): Promise<PlanDetail> {
    const q = version ? `?version=${version}` : "";
    return request<PlanDetail>(`/plans/${encodeURIComponent(id)}${q}`);
  },
  /** 一句话修改；用户确认过的批量删除操作用 ops 带回来（不再问模型）。 */
  revise(id: string, feedback: string, ops?: Record<string, unknown>[]): Promise<PlanResponse> {
    return request<PlanResponse>(`/plans/${encodeURIComponent(id)}/revisions`, {
      method: "POST",
      body: JSON.stringify(ops ? { feedback, ops } : { feedback }),
    });
  },
  /** 手动编辑：把当前行程和一个操作发给后端，返回新行程（不写库）。 */
  edit(id: string, itinerary: Itinerary, op: EditOp): Promise<EditResponse> {
    return request<EditResponse>(`/plans/${encodeURIComponent(id)}/edit`, {
      method: "POST",
      body: JSON.stringify({ itinerary, edit: op }),
    });
  },
  alternatives(id: string, itinerary: Itinerary, placeId: string, indoorOnly = false): Promise<Alternative[]> {
    return request<Alternative[]>(`/plans/${encodeURIComponent(id)}/alternatives`, {
      method: "POST",
      body: JSON.stringify({ itinerary, place_id: placeId, indoor_only: indoorOnly }),
    });
  },
  /** 随行清单的读取：失败就当没有（不显示这张卡），不把页面切成「只读」。 */
  async packing(id: string): Promise<PackingData> {
    return quietGet<PackingData>(`/plans/${encodeURIComponent(id)}/packing`);
  },
  async todos(id: string): Promise<TodosData> {
    return quietGet<TodosData>(`/plans/${encodeURIComponent(id)}/todos`);
  },
  async notes(id: string): Promise<Record<string, string>> {
    return (await quietGet<{ notes: Record<string, string> }>(`/plans/${encodeURIComponent(id)}/notes`)).notes;
  },
  generatePacking(id: string): Promise<PackingData> {
    return request<PackingData>(`/plans/${encodeURIComponent(id)}/packing/generate`, { method: "POST" });
  },
  restorePacking(id: string): Promise<PackingData> {
    return request<PackingData>(`/plans/${encodeURIComponent(id)}/packing/restore`, { method: "POST" });
  },
  addPacking(id: string, category: string, text: string): Promise<PackingData> {
    return request<PackingData>(`/plans/${encodeURIComponent(id)}/packing`, {
      method: "POST",
      body: JSON.stringify({ category, text }),
    });
  },
  patchPacking(id: string, itemId: number, body: { packed?: boolean; text?: string; category?: string }): Promise<PackingData> {
    return request<PackingData>(`/plans/${encodeURIComponent(id)}/packing/${itemId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  },
  deletePacking(id: string, itemId: number): Promise<PackingData> {
    return request<PackingData>(`/plans/${encodeURIComponent(id)}/packing/${itemId}`, { method: "DELETE" });
  },
  addTodo(id: string, body: { text: string; due: string | null; key?: string }): Promise<TodosData> {
    return request<TodosData>(`/plans/${encodeURIComponent(id)}/todos`, { method: "POST", body: JSON.stringify(body) });
  },
  patchTodo(id: string, todoId: number, body: { done?: boolean; text?: string; due?: string | null }): Promise<TodosData> {
    return request<TodosData>(`/plans/${encodeURIComponent(id)}/todos/${todoId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  },
  deleteTodo(id: string, todoId: number): Promise<TodosData> {
    return request<TodosData>(`/plans/${encodeURIComponent(id)}/todos/${todoId}`, { method: "DELETE" });
  },
  /** 给一条行程安排写备注（ref 见 ui.tsx 的 noteRef）；空文本 = 删除。 */
  async putNote(id: string, ref: string, text: string): Promise<Record<string, string>> {
    const r = await request<{ notes: Record<string, string> }>(`/plans/${encodeURIComponent(id)}/notes`, {
      method: "PUT",
      body: JSON.stringify({ ref, text }),
    });
    return r.notes;
  },
  /** 下载 PDF：先在后台生成（有缓存时很快），完成后再触发浏览器下载；失败抛出，由调用方弹窗。 */
  downloadPdf(id: string, version?: number, pagePerDay = true): Promise<void> {
    return downloadFile(api.pdfUrl(id, version, pagePerDay), `${id}${version ? `-v${version}` : ""}.pdf`);
  },
  /** 长图（PNG）：同样先生成再下载（规格 M08 S6）。 */
  downloadImage(id: string, version?: number): Promise<void> {
    const q = version ? `?version=${version}` : "";
    return downloadFile(`${BASE}/plans/${encodeURIComponent(id)}/itinerary.png${q}`, `${id}${version ? `-v${version}` : ""}.png`);
  },
  /** 预算：失败就当没有（不显示预算卡），同样绕开 request()，不把页面切成「只读」。 */
  async budget(id: string, version?: number): Promise<BudgetData> {
    const res = await fetch(`${BASE}/plans/${encodeURIComponent(id)}/budget${version ? `?version=${version}` : ""}`);
    if (!res.ok) {
      // 404 带原因（一日游没有预算数据）；其余当作取不到
      const body = res.status === 404 ? await res.json().catch(() => null) : null;
      const why = body && typeof body.detail === "string" ? body.detail : `预算不可用（HTTP ${res.status}）`;
      throw new ApiError(res.status, why, body);
    }
    return (await res.json()) as BudgetData;
  },
  saveBudgetSettings(id: string, body: BudgetSettingsBody): Promise<BudgetSettingsBody> {
    return request<BudgetSettingsBody>(`/plans/${encodeURIComponent(id)}/budget-settings`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  },
  /**
   * 天气只是锦上添花：任何失败（网络、5xx、数据异常）都当「没有天气」，
   * 而且绕开 request()——它在请求发不出去时会把整个页面切成「离线 · 只读」，不该因为天气连累别的功能。
   */
  async weather(id: string, version?: number): Promise<WeatherData> {
    const none: WeatherData = { available: false, days: [] };
    try {
      const res = await fetch(`${BASE}/plans/${encodeURIComponent(id)}/weather${version ? `?version=${version}` : ""}`);
      if (!res.ok) return none;
      const body = (await res.json()) as WeatherData;
      return body && body.available && Array.isArray(body.days) ? body : none;
    } catch {
      return none;
    }
  },
  /** 保存为新版本（可起名）；后端会按地点库重算购票清单等事实。 */
  saveVersion(id: string, itinerary: Itinerary, name: string, note: string): Promise<PlanResponse> {
    return request<PlanResponse>(`/plans/${encodeURIComponent(id)}/versions`, {
      method: "POST",
      body: JSON.stringify({ itinerary, name, note }),
    });
  },
  /** 回滚 = 以旧版本的内容新建一个版本，旧版本都还在。 */
  restoreVersion(id: string, version: number): Promise<PlanResponse> {
    return request<PlanResponse>(`/plans/${encodeURIComponent(id)}/versions/${version}/restore`, {
      method: "POST",
    });
  },
  attractions(): Promise<AttractionRef[]> {
    return request<AttractionRef[]>("/attractions");
  },
  pdfUrl(id: string, version?: number, pagePerDay = true): string {
    const q = new URLSearchParams();
    if (version) q.set("version", String(version));
    if (!pagePerDay) q.set("page_per_day", "false");
    const qs = q.toString();
    return `${BASE}/plans/${encodeURIComponent(id)}/itinerary.pdf${qs ? `?${qs}` : ""}`;
  },
  icsUrl(id: string, version?: number): string {
    return `${BASE}/plans/${encodeURIComponent(id)}/itinerary.ics${version ? `?version=${version}` : ""}`;
  },
  kmlUrl(id: string, version?: number): string {
    return `${BASE}/plans/${encodeURIComponent(id)}/itinerary.kml${version ? `?version=${version}` : ""}`;
  },
  route(id: string, version?: number): Promise<RouteData> {
    return request<RouteData>(`/plans/${encodeURIComponent(id)}/route${version ? `?version=${version}` : ""}`);
  },
  shareStatus(id: string): Promise<ShareStatus> {
    return request<ShareStatus>(`/plans/${encodeURIComponent(id)}/share`);
  },
  createShare(id: string): Promise<ShareStatus> {
    return request<ShareStatus>(`/plans/${encodeURIComponent(id)}/share`, { method: "POST" });
  },
  setShareOptions(id: string, options: ShareOptions): Promise<ShareStatus> {
    return request<ShareStatus>(`/plans/${encodeURIComponent(id)}/share/options`, {
      method: "PUT",
      body: JSON.stringify(options),
    });
  },
  revokeShare(id: string): Promise<null> {
    return request<null>(`/plans/${encodeURIComponent(id)}/share`, { method: "DELETE" });
  },
  llmUsage(): Promise<LlmUsage> {
    return request<LlmUsage>("/llm/usage");
  },
  /** 清零模型用量统计（只删调用记录，不动行程）。 */
  resetLlmUsage(): Promise<null> {
    return request<null>("/llm/usage", { method: "DELETE" });
  },
  /** 整体替换住宿列表：生成「住宿更新」版本；reschedule = 保存后按新住宿重新排程。 */
  saveStays(id: string, stays: Stay[], reschedule: boolean): Promise<PlanResponse> {
    return request<PlanResponse>(`/plans/${encodeURIComponent(id)}/stays`, {
      method: "PUT",
      body: JSON.stringify({ stays, reschedule }),
    });
  },
  stayRecommendation(id: string): Promise<{ recommendations: AreaRec[]; caveat: string }> {
    return request(`/plans/${encodeURIComponent(id)}/stay-recommendation`);
  },
  async reservations(id: string, version?: number): Promise<ReservationItem[]> {
    const r = await request<{ items: ReservationItem[] }>(
      `/plans/${encodeURIComponent(id)}/reservations${version ? `?version=${version}` : ""}`,
    );
    return r.items;
  },
  saveReservation(
    id: string,
    placeId: string,
    body: {
      status: "todo" | "booked";
      confirmation_code?: string;
      slot?: string;
      note?: string;
      entry_from?: string;
      entry_to?: string;
    },
  ): Promise<ReservationSaved> {
    return request<ReservationSaved>(`/plans/${encodeURIComponent(id)}/reservations/${encodeURIComponent(placeId)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  },
  planTrace(id: string): Promise<TaskTrace> {
    return request<TaskTrace>(`/plans/${encodeURIComponent(id)}/trace`);
  },
  readShare(token: string): Promise<SharedPlan> {
    return request<SharedPlan>(`/share/${encodeURIComponent(token)}`);
  },
};

export type { PlanDetail, PlanResponse };
