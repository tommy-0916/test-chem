export type CampaignMode = "research_preview" | "full_campaign";
export type ExecutionAdapter = "mock" | "manual";
export type WireApi = "chat" | "codex_responses";

export interface HealthResponse {
  status?: string;
  service?: string;
  version?: string;
  detail?: string;
  [key: string]: unknown;
}

export interface CampaignListItem {
  id?: string;
  campaign_id?: string;
  query?: string;
  status?: string;
  phase?: string;
  mode?: CampaignMode | string;
  execution_adapter?: string;
  adapter?: string;
  current_iteration?: number;
  iterations_run?: number;
  max_iterations?: number;
  created_at?: string;
  started_at?: string;
  updated_at?: string;
  finished_at?: string;
  stop_reason?: string;
  goal_reached?: boolean;
  error?: string;
  [key: string]: unknown;
}

export interface CampaignListResponse {
  items: CampaignListItem[];
  total: number;
}

export interface JobState extends CampaignListItem {
  cancel_requested?: boolean;
  progress?: number;
  message?: string;
}

export interface MacroPlanStep {
  "步骤序号"?: number | string;
  "操作"?: string;
  "试剂/对象"?: string;
  "参数"?: string;
  "来源"?: string;
  [key: string]: unknown;
}

export interface SearchHit {
  title?: string;
  file_path?: string;
  score?: number;
  problem?: string;
  synthesis_summary?: string;
  matched_terms?: string[];
  steps?: MacroPlanStep[];
  [key: string]: unknown;
}

export interface ResearchState {
  status?: string;
  current_branch?: string;
  next_branch?: string | null;
  last_completed_branch?: string | null;
  route_message?: string;
  branch_history?: string[];
  stage_route?: string[];
  current_stage?: string;
  current_stage_plan?: string;
  macro_plan?: MacroPlanStep[];
  stage_route_reason?: string;
  current_stage_reason?: string;
  survey_report?: Record<string, unknown>;
  knowledge_hits?: SearchHit[];
  extracted_protocols?: Record<string, unknown>[];
  latest_observation?: Record<string, unknown>;
  observations?: Record<string, unknown>[];
  observation_stage_fit?: Record<string, unknown>;
  observation_interpretation?: Record<string, unknown>;
  stage_progress?: Record<string, unknown>;
  stage_progress_status?: string;
  post_observation_repair_path?: string;
  manual_handoff?: string;
  plan_revisions?: PlanRevision[];
  errors?: string[];
  logs?: string[];
  created_at?: string;
  [key: string]: unknown;
}

export interface PlanRevision {
  campaign_id?: string;
  plan_version?: number;
  event?: string;
  scope?: string;
  trigger?: string;
  branch_path?: string;
  reason?: string;
  observation_summary?: string;
  evidence_refs?: string[];
  agent_status?: string;
  previous_plan?: Record<string, unknown> | null;
  new_plan?: Record<string, unknown> | null;
  recorded_at?: string;
  [key: string]: unknown;
}

export interface WorkflowStep {
  step_number?: number | string;
  workstation?: string;
  operation?: string;
  parameters?: Record<string, unknown>;
  source_macro_step?: number | string;
  notes?: string;
  [key: string]: unknown;
}

export interface DevicePackage {
  status?: string;
  feedback_type?: string;
  exp_id?: string;
  iteration_id?: number;
  workflow_id?: number;
  macro_plan_summary?: string;
  verification_summary?: Record<string, unknown>;
  feasibility?: Record<string, unknown>;
  feasibility_assessment?: Record<string, unknown>;
  device_self_check?: Record<string, unknown>;
  reagent_slot_plan?: Record<string, unknown>[];
  container_plan?: Record<string, unknown>[];
  workflow_txt?: string;
  workflow_json?: {
    steps?: WorkflowStep[];
    offline_handoffs?: Record<string, unknown>[];
    [key: string]: unknown;
  };
  error_package?: {
    type?: string;
    blocking_constraints?: string[];
    message?: string;
    unsupported_items?: Record<string, unknown>[];
    [key: string]: unknown;
  };
  device_capabilities?: Record<string, unknown>;
  errors?: string[];
  logs?: string[];
  terminal_package?: DevicePackage;
  package?: DevicePackage;
  [key: string]: unknown;
}

export interface CampaignSummary {
  campaign_id?: string;
  query?: string;
  stop_reason?: string;
  goal_reached?: boolean;
  iterations_run?: number;
  started_at?: string;
  finished_at?: string;
  adapter?: string;
  plan_versions?: number;
  final_state_path?: string;
  final_report?: string;
  final_report_markdown?: string;
  trace?: Record<string, unknown>[];
  [key: string]: unknown;
}

export interface CampaignIteration {
  iteration?: number;
  phase?: string;
  status?: string;
  research?: Record<string, unknown> | null;
  device?: Record<string, unknown> | null;
  observation?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface CampaignArtifacts {
  summary?: CampaignSummary | null;
  plan_revisions?: PlanRevision[];
  iterations?: CampaignIteration[];
  final_report?: string | null;
  [key: string]: unknown;
}

export interface CampaignLogs {
  lines?: string[];
  cursor?: string | number | null;
  [key: string]: unknown;
}

export interface CampaignDetail {
  job: JobState;
  research?: ResearchState | { state?: ResearchState; [key: string]: unknown } | null;
  device?: DevicePackage | null;
  campaign?: CampaignArtifacts | null;
  logs?: CampaignLogs | null;
  [key: string]: unknown;
}

export interface CreateCampaignInput {
  query: string;
  mode: CampaignMode;
  references: string[];
  execution_adapter: ExecutionAdapter;
  max_iterations: number;
  enable_memory: boolean;
  include_device_context: boolean;
  online_literature: boolean;
  web_search: boolean;
  download_pdfs: boolean;
  wire_api: WireApi;
}

export interface ObservationInput {
  summary: string;
  observation_type?: string;
  status?: string;
  metrics?: Record<string, unknown>;
  notes?: string;
}

export interface UploadResult {
  upload: {
    id: string;
    name: string;
    reference: string;
    size: number;
  };
}
