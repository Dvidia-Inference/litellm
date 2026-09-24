-- CreateIndex
CREATE INDEX IF NOT EXISTS "LiteLLM_SpendLogs_session_id_api_key_startTime_request_id_idx" ON "LiteLLM_SpendLogs"("session_id", "api_key", "startTime" DESC, "request_id");
