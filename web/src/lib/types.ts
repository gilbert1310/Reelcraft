export type UploadStatus = "queued" | "processing" | "done" | "failed" | "link_failed";

export interface Upload {
  id: string; user_id: string; file_name: string | null; storage_path: string | null; source_url: string | null;
  status: UploadStatus; error: string | null; duration: number | null; cut_count: number | null;
  setup_count: number | null; plan_summary: string | null; created_at: string; updated_at: string;
}

export interface Setup {
  id: string; upload_id: string; setup_number: number; description: string | null; angle: string | null;
  subject: string | null; motion: string | null; film_duration_seconds: number | null; beginner_tip: string | null;
  thumbnail_path: string | null;
}

export interface Cut {
  id: string; setup_id: string; setup_number: number; order_index: number; start_time: number; end_time: number;
  transition_in: string; speed: number; action: string | null; thumbnail_path: string | null;
}

export type ProjectStatus = "shooting" | "assembling" | "done" | "failed";
export interface Reshoot { setup_number: number; cut_order_indexes: number[]; reason: string }

export interface Project {
  id: string; upload_id: string; title: string | null; status: ProjectStatus; preview_path: string | null;
  timeline_path: string | null; otio_path: string | null; overall_score: number | null; reshoots: Reshoot[];
  error: string | null; created_at: string;
}

export interface Coverage { cut_id: string; order_index: number; status: "covered" | "weak" | "missing"; reason: string }

export interface Capture {
  id: string; project_id: string; setup_id: string | null; suggested_setup_id: string | null; storage_path: string;
  file_name: string | null; source: "phone" | "import"; check_status: "pending" | "checking" | "pass" | "retry" | "failed";
  check_feedback: string | null; coverage: Coverage[] | null; ai_checked: boolean; created_at: string;
}

export interface CutMatch {
  id: string; cut_id: string | null; order_index: number; score: number | null; fixes: string[] | null;
  flags: string[] | null; thumbnail_path: string | null;
}

export interface WrapMissing {
  cut_id: string; order_index: number; setup_id: string; setup_number: number; what_to_film: string;
  reason: string; target_seconds: number;
}
export interface WrapCheck { missing: WrapMissing[]; unchecked_count: number; all_covered: boolean }

export type RawStatus = "uploading" | "processing" | "ready" | "revising" | "exporting" | "exported" | "failed";
export interface Suggestion { recipe_id: string | null; style_name: string | null; moods: string[] | null; preview_path: string | null }
export interface Revision { instruction: string; preview_path: string; at: string }

export interface RawProject {
  id: string; title: string | null; mood: string | null; status: RawStatus; suggestions: Suggestion[] | null;
  chosen_index: number | null; preview_path: string | null; revisions: Revision[]; final_path: string | null;
  timeline_path: string | null; otio_path: string | null; error: string | null; created_at: string;
}

export interface Recipe {
  id: string; style_name: string | null; genre: string | null; moods: string[]; summary: string | null;
  duration: number | null; cut_count: number | null; setup_count: number | null;
}
