export type UpstreamErrorFactsValue = {
  upstream_status_code?: number | null;
  upstream_error_code?: string | null;
  upstream_error_message?: string | null;
  upstream_error_detail?: string | null;
  upstream_request_id?: string | null;
  client_request_id?: string | null;
  gateway_request_id?: string | null;
};

export function upstreamErrorRequestIds(
  value: UpstreamErrorFactsValue,
): string | null {
  const ids = [
    value.gateway_request_id ? `Gateway Request ID：${value.gateway_request_id}` : null,
    value.upstream_request_id ? `上游 Request ID：${value.upstream_request_id}` : null,
    value.client_request_id ? `Client Request ID：${value.client_request_id}` : null,
  ].filter(Boolean);
  return ids.length > 0 ? ids.join("\n") : null;
}

export function hasUpstreamErrorFacts(value: UpstreamErrorFactsValue): boolean {
  return Boolean(
    value.upstream_status_code
      || value.upstream_error_code
      || value.upstream_error_message
      || value.upstream_error_detail
      || value.upstream_request_id
      || value.client_request_id
      || value.gateway_request_id,
  );
}
