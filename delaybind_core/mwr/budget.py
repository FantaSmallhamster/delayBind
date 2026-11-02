from .common import dump, ResourceLimit, ProtocolError
from . import PROMPT_VERSION


def response_schema(model):
    schema=model.model_json_schema()
    def required_fields(node):
        if isinstance(node,dict):
            # All response fields must be emitted. Local defaults remain useful
            # for checkpoints, but must not hide missing model output fields.
            if 'properties' in node: node['required']=list(node['properties'])
            node.pop('default',None)
            for child in node.values(): required_fields(child)
        elif isinstance(node,list):
            for child in node: required_fields(child)
    required_fields(schema)
    return schema


class CallBudget:
    def __init__(self, client, reader, tokenizer, config, run_id, namespace, log, calls=0):
        self.client, self.reader = client, reader or client
        self.tokenizer, self.config, self.run_id = tokenizer, config, run_id
        self.namespace, self.log, self.calls = namespace, log, calls
        self.repairs = 0
        self.on_meter = lambda: None

    def _estimate(self, client, interface, messages, schema):
        if hasattr(client, "_request_payload") and hasattr(client, "config"):
            payload = client._request_payload(interface, messages, response_schema=schema,
                extra={"max_tokens":self.config.mwr.output_tokens})
        else:
            payload = {"messages":messages,"schema":schema,"max_tokens":self.config.mwr.output_tokens}
        return len(self.tokenizer.encode(dump(payload)))

    def check_request(self, messages, model, *, final=False):
        schema=response_schema(model)
        request = self._messages(messages, schema, final=final)
        estimate = self._estimate(self.client, "ANSWER" if final else "MEMORY", request, schema)
        if self.calls >= self.config.max_model_calls - (0 if final else 1):
            raise ResourceLimit("model call budget; final ANSWER reserved")
        if estimate + self.config.mwr.output_tokens > self.config.mwr.max_prompt_tokens:
            raise ResourceLimit("complete request plus output exceeds token budget")
        return estimate

    def _messages(self, messages, schema, *, final=False):
        namespace = PROMPT_VERSION + ":shared-answer" if final else self.namespace
        # response_format constrains syntax, but need not place field semantics
        # in the model's context. Include the same schema in the counted prompt.
        return [{"role":"system", "content":"MWR request contract " + namespace
            + "\nReturn only the final JSON object. Do not put reasoning, deliberation, or repeated explanations in any field. All listed fields are required. Output schema:\n"
            + dump(schema)}, *messages]

    async def invoke(self, interface, messages, model, *, role="HIGH", final=False, validator=None):
        schema = response_schema(model)
        client = self.client if final else (self.reader if role == "LOW" else self.client)
        # Semantic namespace is in the request; clients cannot reuse legacy caches.
        request = self._messages(messages, schema, final=final)
        for repair in range(self.config.max_response_retries + 1):
            limit = self.config.max_model_calls - (0 if final else 1)
            if self.calls >= limit:
                self.log("BUDGET_BLOCKED", {"reason": "model_calls", "calls": self.calls})
                raise ResourceLimit("model call budget; final ANSWER reserved")
            estimate = self._estimate(client, interface, request, schema)
            if estimate + self.config.mwr.output_tokens > self.config.mwr.max_prompt_tokens:
                self.log("BUDGET_BLOCKED", {"reason": "request_tokens", "estimated_input": estimate})
                raise ResourceLimit("complete request plus output exceeds token budget")
            self.calls += 1
            self.on_meter()
            self.log("MODEL_CALL", {"interface": interface, "role": role, "logical_call": self.calls,
                     "repair": repair, "estimated_input_tokens": estimate,
                     "reserved_output_tokens": self.config.mwr.output_tokens,
                     "counting": "local serialized request estimate; provider wrapper unknown"})
            raw = await client.complete(run_id=self.run_id, interface=interface, agent_role=role,
                       messages=request, response_schema=schema, extra={"max_tokens": self.config.mwr.output_tokens})
            try:
                parsed=model.model_validate_json(raw)
                return validator(parsed) if validator else parsed
            except ValueError as exc:
                self.log("FORMAT_ERROR", {"interface": interface, "error": str(exc)[:500]})
                if repair >= self.config.max_response_retries:
                    raise ProtocolError("invalid " + interface + " response") from exc
                self.repairs += 1
                # Do not persist malformed output as semantic memory.
                request = [*request, {"role": "user", "content": "Invalid response: " + str(exc)[:400]
                    + ". Return a concise complete JSON object conforming to the schema, without reasoning text."}]
