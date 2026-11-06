export type Severity = "error" | "warning" | "info";

export interface Finding {
  code: string;
  severity: Severity;
  message: string;
  day: string | null;
  item_index: number | null;
}

export interface ValidationReport {
  findings: Finding[];
}

export interface ItineraryItem {
  time: string;
  title: string;
  type: string;
  location: string;
  duration_minutes: number;
  transport: string | null;
  booking_required: boolean;
  notes: string | null;
  place_id?: string | null;
  locked?: boolean; // 用户锁定：重新排程时位置和开始时间不变
  look_for?: string[]; // 深度路线：这一站看什么（策展数据，带来源）
  why_here?: string; // 深度路线：这一站为什么在这条线上
  city_id?: string | null; // 多城市行程：这一条在哪座城市
}

export interface Day {
  date: string;
  theme: string;
  city_id?: string | null; // 多城市行程：这一天在哪座城市（换城市那天是后一座，看 theme_by_city）
  theme_by_city?: Record<string, string>; // 换城市那天：两座城市各自的主题
  items: ItineraryItem[];
  chapter?: string | null; // 深度路线：「第 1 章 · 王室收藏与私人藏家」
  guide?: string | null; // 深度路线：这一天的导览
}

export interface TaskStep {
  name: string;
  state: "pending" | "running" | "done" | "failed";
  started_at: string | null;
  finished_at: string | null;
}

/** 后台生成任务（GET /tasks/{id}）：状态和每一步的进度都是后端记录的真实数据。 */
export interface Task {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  theme_id: string;
  steps: TaskStep[];
  plan_id: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

/** 「正在生成」区的一张卡（GET /tasks?unfinished=true）：没生成完的任务，加 24 小时内失败的。 */
export interface TaskCard {
  id: string;
  status: Task["status"];
  theme_id: string;
  theme_name: string;
  destination: string;
  start_date: string;
  end_date: string;
  days: number;
  steps: TaskStep[];
  current_step: string | null;
  steps_done: number;
  steps_total: number;
  plan_id: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

/** 「我的行程」里的一张卡片。 */
export interface PlanCard {
  plan_id: string;
  title: string;
  theme_id: string | null;
  theme_name: string | null;
  start_date: string;
  end_date: string;
  days: number;
  latest_version: number;
  created_at: string;
  errors: number;
  warnings: number;
  narration: "template" | "llm" | null;
  archived: boolean;
  copied_from: string | null;
  city_id: string;
  hero: (PlaceImageInfo & { place_id: string }) | null; // 封面大图（这趟行程里标志性地点的大图 + 署名）
  cities?: CityHero[]; // 多城市行程：每座城市一张封面图，卡片上轮流切换
}

/** 多城市行程里的一座城市。 */
export interface CityHero {
  city_id: string;
  name_zh: string;
  hero: (PlaceImageInfo & { place_id: string }) | null;
}

/** 多城市行程的城市段（PlanDetail.cities）。 */
export interface PlanCity {
  city_id: string;
  name_zh: string;
  start_date: string;
  end_date: string;
}

/** 已支持的城市（GET /cities，规格 M17）。hero = 这座城市的代表大图。 */
/** 认目的地（规格 10）：候选城市。 */
export interface DestinationChoice {
  qid: string;
  label: string;
  country: string;
  description: string;
}

export interface DestinationResolve {
  status: "ready" | "build" | "choices" | "unknown";
  city_id?: string;
  name_zh?: string;
  choice?: DestinationChoice;
  choices?: DestinationChoice[];
  many?: boolean; // 国家里最常去的城市：可以多选、按点选顺序成多城市行程；同名的地方只能选一个
  reason?: string;
}

export interface DestinationJob {
  status: "running" | "done" | "failed";
  city_id: string;
  name_zh?: string;
  message: string;
  error: string;
}

export interface CityInfo {
  id: string;
  name_zh: string;
  name_en: string;
  tier?: "full" | "standard" | "light"; // light = 周边一日游的目的地；standard = 现建的（价格 / 预约待核对）
  country: string;
  currency: string;
  timezone: string;
  center: [number, number];
  map_provider: string;
  residency_options: string[];
  suggested_days: { min: number; max: number; quality_max: number; fill_max?: number | null; note: string };
  hero: (PlaceImageInfo & { place_id: string }) | null;
}

/** 购票方式：必须线上 / 线上或现场 / 无需购票。 */
export type Ticketing = "online_required" | "online_or_walkin" | "none";

export interface BookingItem {
  title: string;
  reason: string;
  deadline_hint: string | null;
  url: string | null;
  place_id?: string | null;
  must_online?: boolean; // true: 必须提前线上购票；false: 线上购票，现场也可排队
  note?: string | null;
  url_status?: "ok" | "bot_protected" | null;
  verified_at?: string | null;
}

/** 这份行程是怎么生成的（主题包 + 排程器）。 */
export interface GenerationInfo {
  theme_id: string;
  theme_name: string;
  themes?: string[];
  narration: "template" | "llm";
  narration_note: string | null;
  notes: string[];
  day_reasons: Record<string, string[]>;
  travel_estimated: boolean;
  route?: RouteInfo | null; // 深度路线（规格 09）
}

/** GET /themes 的一条。 */
export interface ThemeInfo {
  id: string;
  name_zh: string;
  tagline: string;
  description: string;
  tips: string[];
  anchors_per_day: Record<string, number>;
  place_count: number;
}

/** GET /themes/{id}：主题和它的成员（候选池面板用）。 */
export interface ThemeDetail extends ThemeInfo {
  members: { place_id: string; role: "anchor" | "support"; weight: number }[];
}

/** GET /places/{id}：地点详情卡。 */
export interface PlaceDetail extends PlaceDto {
  stories?: CultureStory[];
  best_time: string;
  price_eur: number | null;
  ticketing_checked_at?: string | null;
  themes: { id: string; name_zh: string; role: "anchor" | "support"; weight: number }[];
  verification: {
    coords_verified: boolean;
    hours_verified: boolean;
    verified_at: string | null;
    url_status: "ok" | "bot_protected" | null;
    ticketing_checked_at: string | null;
  };
}

/** 地点照片 + 署名、简介 + 来源（规格 M02 S7b）；照片文件由后端 /media/places/* 提供。 */
export interface PlaceImageInfo {
  url: string; // 相对后端地址的路径，用 mediaSrc() 拼成完整地址
  title: string;
  author: string;
  license: string;
  license_url: string | null;
  source_url: string; // Wikimedia Commons 文件页
  width: number;
  height: number;
}
export interface PlaceIntroInfo {
  text: string;
  source_url: string;
  source_name: string;
  license: string;
  license_url: string;
}
export interface PlaceMediaInfo {
  image?: PlaceImageInfo;
  intro?: PlaceIntroInfo;
  hero?: PlaceImageInfo; // 封面大图（≥ 1600 宽，规格 M02 S7c）；只有标志性地点有
}

/** 自然语言填表的结果（规格 M01 S10–S13）：fields 只含读到的项。 */
export interface NlIntent {
  fields: {
    start_date?: string;
    end_date?: string;
    days?: number;
    theme_ids?: string[];
    travelers?: { age: number; residence: string; student: boolean }[];
    budget_cap?: number;
    currency?: "CNY" | "EUR";
    travel_style?: "relaxed" | "balanced" | "intense";
    must_go_ids?: string[];
    avoid_ids?: string[];
    must_go?: string[]; // 上面两项对应的地点中文名，填进「偏好」里的文字框
    avoid?: string[];
    restrictions?: string[];
    destination?: string; // 读到的目的地（可能不在库里，提交时再去认）
    origin?: string; // 出发地：「从上海宝山出发」→ 填进去程「出发地」和返程「回到哪里」
    transport_out?: TransportMode; // 去程 / 返程的交通方式：「高铁来回」两头都是高铁
    transport_back?: TransportMode;
    /** 多城市（规格 M01 S14）：按顺序的每座城市；开始日期读到了才有日期（换城市默认在第二天） */
    stops?: { destination: string; days?: number; start_date?: string; end_date?: string }[];
  };
  understood: string[];
  missing: string[];
  notes: string[];
  model_used: boolean;
}

/** GET /places 的一条（地点库，含核对记录）。 */
export interface PlaceDto {
  id: string;
  name_zh: string;
  name_en: string;
  name_local?: string | null; // 当地语言名（规格 07 P18），老数据可能没有
  local_language?: string | null; // 目的地的官方语言；是 zh（国内）就只显示中文名
  fame?: { sitelinks: number; source: string; checked_at: string; rank?: number; of?: number; views?: number } | null; // 名气分 + 热度（M02 S9）
  voyage?: { kind: string; url: string; revid: number; checked_at: string } | null; // 维基导游列了这个地点
  name_local_check?: { source: string; status: "verified" | "draft"; checked_at: string; wikidata: string | null } | null;
  data_status?: "verified" | "draft"; // 开放数据草稿（周边一日游目的地）还是已核对的策展数据
  season?: { start: string; end: string; source_url: string; checked_at: string; confirmed_by: "official" | "listing"; note: string | null } | null;
  closed_dates?: string[];
  date_notes?: Record<string, string>; // 「MM-DD」→ 官网写明的那天提醒（提前闭馆、改时间）
  category: string;
  zone: string;
  area: string;
  lat: number;
  lng: number;
  duration_min: number;
  closed_weekdays: number[];
  long_term_closed: boolean;
  booking: { required: boolean; lead_time_hint: string | null };
  official_url: string | null; // 只有有线上购票渠道的地点才有；其余只有 address
  address: string | null;
  ticketing: Ticketing;
  ticket_note: string | null;
  url_status: "ok" | "bot_protected" | null;
  verified_at: string | null;
  metro: string | null;
  highlights: string[];
  note: string;
  interests: string[];
  media?: PlaceMediaInfo | null; // 没有图片 / 简介的地点是 null
}

export interface Itinerary {
  trip: {
    destination: string;
    date_range: { start: string; end: string };
    traveler_profile: { style: string; budget: string; interests: string[] };
  };
  days: Day[];
  booking_checklist: BookingItem[];
  risk_notes: string[];
  version: string;
  generation?: GenerationInfo | null;
  legs?: { city_id: string }[] | null; // 多城市行程：每座城市一块（有它就是多城市）
}

export interface VersionRecord {
  version: number;
  created_at: string;
  trigger: string;
  name?: string; // 用户给这个版本起的名字
  note: string;
  change_summary: string;
  errors_before: number;
  errors_after: number;
}

export interface RevisionChange {
  summary: string;
  changed_days: string[];
  added: string[];
  removed: string[];
  applied?: string[]; // 一句话修改：做成了的改动
  skipped?: string[]; // 没做成的操作和原因
}

/** 手动编辑的操作（和后端 app/editing.py 一一对应）。 */
export type EditOp =
  | { op: "move"; place_id: string; to_date: string }
  | { op: "reorder"; date: string; place_ids: string[] }
  | { op: "lock"; place_id: string; locked: boolean }
  | { op: "remove"; place_id: string }
  | { op: "add"; place_id: string; date: string }
  | { op: "swap"; place_id: string; new_place_id: string }
  | { op: "reschedule" };

export interface EditResponse {
  itinerary: Itinerary;
  report: ValidationReport;
  touched: string[];
  notes: string[];
}

/** 随行清单（规格 M10 S2–S4）。 */
export interface PackingItem {
  id: number;
  key: string | null; // 推荐项的编号；自己加的为空
  category: string;
  text: string;
  packed: boolean;
  position: number;
  source_note?: string; // 有事实依据的条目（护照有效期、插头）才有来源
  source_url?: string;
  checked_at?: string;
}
export interface PackingData {
  items: PackingItem[];
  packed: number;
  total: number;
  categories: string[];
  generated: boolean;
  hidden: number; // 被删掉藏起来的推荐项数
  added?: number;
  restored?: number;
  basis?: string;
  climate?: string;
}
export interface TodoUrgency {
  level: "done" | "none" | "overdue" | "today" | "soon" | "ok";
  days: number | null;
}
export interface TodoItem {
  id: number;
  key: string | null;
  text: string;
  due: string | null;
  done: boolean;
  urgency: TodoUrgency;
}
export interface TodoSuggestion {
  key: string;
  text: string;
  due: string;
  why: string;
}
export interface TodosData {
  today: string;
  items: TodoItem[];
  summary: { open: number; done: number; urgent: number; overdue: number };
  suggestions: TodoSuggestion[];
}

/** 预算（规格 M09 S1 / S2）。 */
export type Currency = "CNY" | "EUR";
export interface Traveler {
  name?: string; // 选填，随行程保存；不进分享页 / 导出
  age: number;
  residence: "none" | "fr" | "eu"; // 不长居欧洲 / 长居法国 / 长居其他欧盟国家
  student: boolean;
}
/** 编辑中的出行人：年龄可以先空着（新添加的出行人默认不填），提交前必须填完。 */
export type TravelerDraft = Omit<Traveler, "age"> & { age: number | null };
export interface BudgetGap {
  level: "large" | "unreachable"; // large = 超出上限一半以上；unreachable = 就算门票全免也不够
  non_ticket: number; // 门票以外的费用（餐饮 / 交通 / 住宿），以结算货币计
  tickets: number; // 门票合计，以结算货币计
  over_ratio: number | null;
}
export interface BudgetCategory {
  key: "tickets" | "meals" | "transport" | "lodging";
  label: string;
  source: "library" | "estimate" | "official" | "user";
  source_note: string;
  eur_person: number;
  eur_total: number;
  cny_person: number | null;
  cny_total: number | null;
  pending: string[]; // 待补：没价格的景点 / 没填价格的住宿晚数（不当 0）
  detail: string | null;
  share: number; // 占总额的比例 0–1
  unknown: boolean; // 一个价格都没有（全是待补）：显示「待补」而不是 0
}
export interface BudgetSuggestion {
  place_id: string;
  title: string;
  cost_eur: number; // 这批出行人合计的门票
  alt_place_id: string;
  alt_title: string;
  alt_cost_eur: number;
  saving_eur: number;
  saving: number; // 以结算货币计
}
export interface BudgetDiscount {
  traveler: number;
  name?: string | null;
  who: string;
  saved_eur: number;
  places: string[];
}
export interface BudgetData {
  currency: Currency; // 结算货币：金额以它为主，另一种并列
  days: number;
  travelers: (Traveler & { label: string })[];
  cap: { amount: number; currency: Currency } | null;
  meal: { value: number; default: number; is_override: boolean; level: string; level_zh: string };
  fx: { rate: number; date: string; stale: boolean } | null;
  categories: BudgetCategory[];
  discounts: BudgetDiscount[];
  total: { eur_person: number; eur_total: number; cny_person: number | null; cny_total: number | null };
  pending_count: number;
  status: "none" | "within" | "over" | "unknown";
  status_amount: number | null;
  status_currency: Currency | null;
  suggestions: BudgetSuggestion[];
  gap: BudgetGap | null;
  notes: string[];
}
export interface BudgetSettingsBody {
  travelers?: Traveler[];
  currency?: Currency;
  cap_amount?: number | null;
  cap_currency?: Currency;
  meal_eur_per_day?: number | null;
}

/** 天气（规格 M10 S1）：每个行程日一条；source=history 是去年同一天，只作参考。 */
export interface WeatherDay {
  date: string;
  source: "forecast" | "history";
  code: number;
  label: string;
  icon: string;
  t_max: number | null;
  t_min: number | null;
  rain_prob: number | null;
  rain_mm: number | null;
  rainy: boolean; // 预报降雨概率 ≥ 60%
  outdoor_places: { place_id: string; title: string }[];
  mixed_places: { place_id: string; title: string }[];
}
export interface WeatherData {
  available: boolean;
  days: WeatherDay[];
  note?: string;
}

/** 「换一个」的备选。 */
export interface Alternative {
  place_id: string;
  name_zh: string;
  area: string;
  address: string | null;
  category: string;
  duration_min: number;
  ticketing: string;
  reason: string;
}

/** 一句话修改要删 3 处及以上时，后端先要求确认（409）。 */
export interface BulkConfirm {
  needs_confirm: true;
  message: string;
  preview: string[];
  ops: Record<string, unknown>[];
}

export interface PlanResponse {
  plan_id: string;
  version: number;
  itinerary: Itinerary;
  report: ValidationReport;
  repair_rounds: number;
  change: RevisionChange | null;
  conflicts: ShiftConflict[]; // 平移日期后新出现的闭馆日冲突
  stays: Stay[];
  notes: string[]; // 保存住宿时的提示（地址没定位到……）
}

/** 文件附件（规格 M10 S5）。 */
export interface Attachment {
  id: string;
  name: string;
  kind: "pdf" | "png" | "jpg";
  size: number;
  target_kind: "plan" | "place" | "day" | "reservation";
  target_ref: string | null;
  uploaded_at: string;
  deleted_at: string | null;
}
export interface AttachmentsData {
  attachments: Attachment[];
  trash: Attachment[];
  limits: { max_bytes: number; max_files: number; trash_days: number; kinds: string[] };
}

/** 笔记提纯出的一条提示（规格 M12 S4）：来自规则，不是模型；conflict 非空 = 和地点库对不上。 */
export interface NoteTip {
  kind: "booking" | "queue" | "duration" | "closed" | "cash" | "safety" | "price";
  label: string;
  text: string;
  conflict: string | null;
}

export interface NoteCandidate {
  place_id: string;
  name_zh: string;
  area: string;
}

/** 确认页里「在地点库里」的一行；置信度 low 时 place_id 为空，让用户从 candidates 里选。 */
export interface ParsedPlace {
  mention: string;
  place_id: string | null;
  name_zh: string | null;
  area: string | null;
  confidence: "high" | "medium" | "low";
  candidates: NoteCandidate[];
  snippet: string;
  tips: NoteTip[];
  complaint: boolean;
  closed_long_term: boolean;
}

/** 笔记里提到、地点库里没有的地点：事实未核对，只显示，不进排程。 */
export interface PendingPlace {
  name: string;
  snippet: string;
}

/** 高德搜到的库外地点（`GET /places/lookup`）；只有名字、地址、类型和坐标，没有任何经营信息。 */
export interface LookupHit {
  id: string; // 高德的编号：只用来在列表里区分，不保存
  name: string;
  address: string;
  area: string; // 所在区
  type: string;
  category: string;
  lat: number;
  lng: number;
}
export interface LookupResult {
  hits: LookupHit[];
  reason?: "no_key" | "quota" | "unreachable" | "not_amap_city";
}

/** 相邻两点之间高德算出的通勤（`POST /commute`）；只显示、不保存。 */
export interface CommuteLeg {
  minutes: number;
  mode: string;
  distance_m: number;
  steps: string[];
}
export interface CommuteResult {
  legs: (CommuteLeg | null)[];
  reason?: "no_key" | "quota" | "unreachable" | "not_amap_city";
}

/** 某个地点是从哪段笔记来的；随行程需求保存。 */
export interface NoteSource {
  place_id: string;
  snippet: string;
  tips: NoteTip[];
}

export interface ParseResult {
  matched: ParsedPlace[];
  pending: PendingPlace[];
  model_used: boolean;
  dropped_instructions: number;
  source: "text" | "url";
  source_host: string | null;
}

/** 一家住宿（规格 M06 S5）：用户自己填的，不是策展数据。 */
export interface Stay {
  name: string;
  check_in: string;
  check_out: string;
  area: string | null;
  address: string | null;
  lat: number | null;
  lng: number | null;
  nightly_eur: number | null;
  note: string | null;
}

/** PUT /plans/{id}/reservations/{place} 的返回：这一行 + 如果锁定 / 解锁了行程，新版本号。 */
export interface ReservationSaved extends ReservationItem {
  version: number | null;
  notes: string[];
}

export interface AreaRec {
  area: string;
  avg_km: number;
  within_2km: number;
  n_places: number;
  reason: string;
}

/** GET /plans/{id}/reservations 的一行：购票清单项 + 我的预订状态 + 最晚订票日。 */
export interface ReservationItem {
  place_id: string;
  title: string;
  must_online: boolean;
  in_itinerary: boolean;
  visit_date: string | null;
  lead_days: number | null;
  lead_basis: string | null;
  book_by: string | null;
  urgency: "booked" | "past" | "none" | "late" | "soon" | "ok";
  status: "todo" | "booked";
  confirmation_code: string | null;
  slot: string | null;
  note: string | null;
  entry_from: string | null; // 入馆时间（时段票 = 时段开始）
  entry_to: string | null; // 时段结束
  locked: boolean; // 行程里这个景点是否由这笔预订锁定
  updated_at: string | null;
  booked_for: string | null;
  date_changed: boolean;
  url: string | null;
}

export interface ShiftConflict {
  code: string;
  day: string | null;
  title: string | null;
  message: string;
}

export interface DayTripInfo {
  city: string; // 「海德堡 Heidelberg」
  origin: string; // 「斯图加特 Stuttgart」
}

export interface PlanDetail {
  plan_id: string;
  latest_version: number;
  version: number;
  itinerary: Itinerary;
  report: ValidationReport;
  history: VersionRecord[];
  archived: boolean;
  copied_from: string | null;
  stays: Stay[];
  note_sources: NoteSource[];
  note_pending: PendingPlace[];
  city_id?: string; // 这份行程在哪座城市（周边一日游的目的地不是巴黎）
  day_trip_from?: string | null; // 周边一日游的出发地（有值：不显示住宿卡和随行清单，预算只给说明）
  day_trip_info?: DayTripInfo | null; // 一日游的显示用名字（中文 + 当地语言）
  route_status?: RouteStatus | null; // 深度路线：{id, name_zh, deviated}
  stories?: Record<string, CultureStory[]>; // 地点 → 它出现在哪些作品里（读取时现算）
  culture?: CultureRecap[]; // 这趟旅行走过的作品
  cities?: PlanCity[]; // 一份多城市行程里的各座城市（单城市行程没有）
  transfers?: PlanTransfer[]; // 一份多城市行程里城市之间的交通
}

/** 多城市行程里换城市的交通（第 position 项 = 第 position 座 → 第 position+1 座）。 */
export interface PlanTransfer {
  position: number;
  from_city: string;
  to_city: string;
  from_name: string;
  to_name: string;
  mode: TransportMode;
  number: string | null;
  estimated: boolean;
  depart_at: string;
  arrive_at: string;
}

/** POST /plans 422 时的 detail 结构。 */
export interface IntakeGap {
  message: string;
  questions: string[];
  missing_fields: string[];
  errors: string[];
}

/** GET /attractions 的一条：景点白名单，带经纬度和实地贴士。 */
export interface AttractionRef {
  name_zh: string;
  name_en: string;
  name_local?: string | null;
  arr: number;
  type: string;
  duration_min: number;
  metro: string;
  closed: string | null;
  booking: boolean;
  interests: string[];
  note: string;
  lat: number;
  lng: number;
  url: string | null; // 官网购票链接：只有有线上购票渠道的地点才有
  address?: string | null;
  ticketing?: Ticketing;
  ticket_note?: string | null;
  highlights?: string[];
  long_term_closed: boolean;
  place_id?: string;
  area?: string;
  url_status?: "ok" | "bot_protected" | null;
  verified_at?: string | null;
  media?: PlaceMediaInfo | null;
}

/** 模型调用的汇总（GET /llm/usage、GET /tasks/{id}/trace）。 */
export interface LlmTotals {
  calls: number;
  failures: number;
  avg_latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
}
export interface LlmCall {
  at: string;
  task_id: string | null;
  purpose: string;
  backend: string;
  model: string;
  ok: boolean;
  error: string | null;
  latency_ms: number;
  prompt_chars: number;
  response_chars: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  cost_usd: number;
}
export interface LlmUsage {
  totals: LlmTotals;
  by_purpose: (LlmTotals & { purpose: string })[];
  recent: LlmCall[];
}
export interface TaskTrace {
  task_id: string;
  status: string;
  plan_id: string | null;
  steps: { name: string; state: string; seconds: number | null }[];
  llm: { totals: LlmTotals; by_purpose: (LlmTotals & { purpose: string })[] };
  calls: LlmCall[];
  degraded: boolean;
  degraded_reason: string | null;
}

/** GET /plans/{id}/route：每天的路线（核对过的坐标、颜色、直线距离、Google 导航链接）。 */
export interface RouteStop {
  place_id: string;
  name: string;
  lat: number;
  lng: number;
  time: string;
  address: string | null;
  name_local?: string | null; // 当地语言名（和中文名没区别时为 null）
}
export interface RouteDay {
  date: string;
  color: string;
  distance_km: number;
  google_urls: string[];
  amap_urls?: string[]; // 境内的点：高德「多点标注」链接（一条最多 10 个点）
  stops: RouteStop[];
}
export interface RouteData {
  total_km: number;
  place_count: number;
  ticket_currency?: string; // 票价的币种（地点库里的票价现在都是欧元）
  ticket_eur?: number | null; // 门票合计（仅地点库里有票价的点）；一个票价都没有 = null
  days: RouteDay[];
}

/** 分享时一起分享什么（规格 M08 S5b）：默认都不分享。 */
export interface ShareOptions {
  packing: boolean;
  todos: boolean;
  notes: boolean;
}

export interface ShareStatus {
  active: boolean;
  token: string | null;
  path: string | null;
  options: ShareOptions;
}

/** GET /share/{token}：公开只读页的内容。 */
export interface SharedPlan {
  itinerary: Itinerary;
  stories?: Record<string, CultureStory[]>;
  city_id?: string;
  version: number;
  route: RouteData;
  weather?: WeatherData; // 分享页也带每天的天气（规格 M10 S1g）；取不到时 available=false
  shared?: (keyof ShareOptions)[]; // 分享的人勾了哪些；没勾的字段根本不在响应里
  packing?: {
    items: { category: string; text: string; packed: boolean }[];
    categories: string[];
    packed: number;
    total: number;
  };
  todos?: { text: string; due: string | null; done: boolean }[];
  notes?: Record<string, string>; // {备注对象: 文本}
}

/** 来回交通（规格 07 P07）：方式 + 出发 / 到达时间（当地时间）+ 选填的出发地、目的地、航班号 / 车次。 */
export type TransportMode = "flight" | "high_speed_rail" | "train" | "coach" | "drive" | "other";
export interface TransportLegDraft {
  mode: TransportMode;
  depart_at: string; // datetime-local，空字符串 = 没填
  arrive_at: string;
  place: string; // 去程 = 出发地；返程 = 目的地
  number: string;
}

// ---------------------------------------------------------------------------
// 周边一日游（规格 08 3.4）
// ---------------------------------------------------------------------------
export interface DayTripStation {
  name: string;
  name_zh: string;
  lat: number;
  lng: number;
}

export interface DayTripOrigin {
  id: string;
  name_local: string;
  name_zh: string;
  country: string;
  station: DayTripStation;
}

export interface DayTripSeasonal {
  id: string;
  name_zh: string;
  name_local: string | null;
  label: string;
  start: string;
  end: string;
  in_season: boolean;
  confirmed_by: "official" | "listing";
  source_url: string;
  note: string | null;
  hours: string;
}

export interface DayTripRide {
  label: string; // ICE 2090 / RE1
  mode: string;
  category: "local" | "long" | "other";
  mode_zh: string;
  minutes: number;
}

/** 一个出行方案：kind = fastest（时刻表上最快，可能要坐 ICE / IC）或 regional（只坐区间 / 本地交通）。 */
export interface DayTripOption {
  kind: "fastest" | "regional";
  minutes: number; // 从住址出发的门到站用时
  transfers: number;
  depart: string;
  arrive: string;
  rides: DayTripRide[]; // 离线回退时为空（没有区分车型）
  station_from: string;
  covered: boolean; // Deutschlandticket 通常覆盖
  needs_ticket: boolean | null; // null = 不知道（离线回退）
  long_distance: boolean;
  fare_note: string;
  db_url: string; // DB 的搜索页
  db_query: string; // 去那里要填的：起点 → 终点，日期时间
}

export interface DayTripDestination {
  city: { id: string; name_zh: string; name_local: string; country: string; region: string | null };
  station: DayTripStation;
  options: DayTripOption[];
  default_kind: "fastest" | "regional";
  cross_border: boolean;
  highlights: { id: string; name_zh: string; name_local: string | null; label: string }[];
  seasonal: DayTripSeasonal[];
  places: number;
  drafts: number;
}

export interface DayTripList {
  origin: { city_local: string; city_zh: string | null; country: string };
  date: string;
  depart: string;
  destinations: DayTripDestination[];
  offline?: boolean; // 时刻表服务连不上，用的是之前存好的「市中心车站」用时
  missing?: string[]; // 这次没查到的城市 id（时刻表服务太忙）
  source: string;
  source_url: string;
  dticket_url: string;
}

/** 出发地：城市（显示用）+ 住址坐标（只在你的浏览器里和查路线时用，不进行程）。 */
export interface DayTripHome {
  label: string; // 「Königstraße 28, 70173 Stuttgart」或「Stuttgart Hbf（市中心车站）」
  city: string;
  city_zh: string | null;
  country: string;
  lat: number;
  lng: number;
  preset?: string; // 斯图加特 / 亚琛的快捷入口：时刻表服务连不上时回退到存好的时刻表
}

export interface AddressCandidate {
  label: string;
  city: string;
  city_zh: string | null;
  country: string;
  lat: number;
  lng: number;
  kind?: "ADDRESS" | "PLACE" | "STOP"; // 地址 / 地点 / 车站
}

/** 一个地点出现在哪部作品里（规格 09 十五）：行程条目、地点卡、今日页、导出都用它。 */
export interface CultureStory {
  work_id: string;
  kind: "film" | "documentary" | "book" | "podcast" | "food"; // food = 这个场所的招牌（规格 09 十七）
  kind_zh: string;
  title: string;
  title_zh: string | null;
  creator: string; // 招牌没有
  year: number | null;
  note: string;
  source: { title: string; url: string };
}

/** 这趟旅行走过的作品：只列至少有一个地点被排进行程的作品。 */
export interface CultureRecap {
  id: string;
  kind: CultureStory["kind"];
  kind_zh: string;
  title: string;
  title_zh: string | null;
  creator: string;
  year: number;
  summary: string; // 自动收录的没有
  visited: { place_id: string; name: string; name_zh: string; note: string }[];
  source: { title: string; url: string; checked_at: string };
}

/** 文化入门（规格 09 十三）：一部电影 / 纪录片 / 书 / 播客，和它带你看的地方。 */
export interface CultureWork {
  id: string;
  kind: "film" | "documentary" | "book" | "podcast";
  kind_zh: string;
  title: string;
  title_zh: string | null;
  creator: string;
  year: number;
  summary: string; // 自动收录的没有
  places: { place_id: string; name: string; name_zh: string; note: string }[];
  routes: { id: string; name_zh: string }[];
  source: { title: string; url: string; checked_at: string };
}

/** 美食（规格 09 十七）：一样名菜名点，有招牌的带场所。 */
/** 版本足迹的一条更新（GET /changelog，新→旧，第一条 current）。 */
export interface ChangelogEntry {
  date: string;
  title: string;
  points: string[];
  current: boolean;
}

export interface FoodDish {
  id: string;
  kind: "bread" | "pastry" | "dish" | "drink";
  kind_zh: string;
  name_zh: string;
  name_local: string;
  summary: string;
  source: { title: string; url: string; checked_at: string };
  places: { place_id: string; name: string; note: string; source: { title: string; url: string } }[];
  city_zh?: string; // 拼搜索链接用
  city_local?: string; // 外国城市的当地语言名（「Napoli」），Google 地图搜当地餐厅用
  near?: [number, number]; // 城市中心（纬度, 经度）：决定用高德还是 Google 地图
  generated?: boolean; // 现建城市的名菜（取自中文维基百科的饮食分类），没有招牌场所
  category?: string;
  category_note?: string; // 卡底来源说明：取自哪个（哪几个）维基分类
}

/** 深度游学路线（规格 09）。 */
export interface RouteCard {
  id: string;
  name_zh: string;
  tagline: string;
  thesis: string;
  days: { min: number; suggested: number; max: number };
  base_theme: string;
  track: "history" | "folk" | "food" | "other";
  track_zh: string;
  chapter_count: number;
  stop_count: number;
  core_count: number;
  examples: string[];
  chapter_titles: string[];
  season: { start: string; end: string } | null; // 限定：只在这段日期里才有（路线上每一站都是当季活动）
}

export interface RouteStopView {
  place_id: string;
  name: string;
  name_zh: string;
  priority: "core" | "optional";
  why_here: string | null;
}

export interface RouteReadingItem {
  title: string;
  kind: "book" | "film";
  creator: string;
  year: number;
  note: string;
  source: { title: string; url: string; checked_at: string };
}

export interface RouteDetail extends RouteCard {
  chapters: { id: string; title: string; story: string; stops: RouteStopView[] }[];
  reading: RouteReadingItem[];
  notes: string[];
}

/** 行程里记的「跟的是哪条路线」（generation.route）：页面显示延伸阅读、判断有没有偏离。 */
export interface RouteInfo {
  id: string;
  name_zh: string;
  thesis: string;
  reading: { title: string; kind: "book" | "film"; creator: string; year: number; note: string; source_url: string }[];
  baseline: Record<string, string[]>;
  skipped: string[];
  notes: string[];
}

export interface RouteStatus {
  id: string;
  name_zh: string;
  deviated: boolean;
}

/** GET /map/usage：高德本月某一类服务的用量（规格 M05 S7c）。 */
export interface MapUsageRow {
  service: string;
  label: string;
  used: number;
  cap: number; // 自留上限：到了当月不再调高德
  free_allowance: number; // 个人认证的免费额度
  warn: boolean;
  blocked: boolean;
}
