import { CairnClient, type RetrievalResponse } from "@cairn/client";

const client = new CairnClient("https://example.test", "key");
const response: RetrievalResponse = await client.retrieve({
  targets: [{ knowledge_base_id: "kb_1" }],
  query: "test",
});
console.log(response.request_id);
