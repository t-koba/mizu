/** Mizu's only trusted Pi extension. No host filesystem or shell tool is exposed. */
import { readFileSync } from "node:fs";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { register } from "./register.mjs";

export default function (pi: ExtensionAPI) {
  const path = process.env.MIZU_BRIDGE_CONFIG;
  if (!path) throw new Error("MIZU_BRIDGE_CONFIG is required");
  register(pi, Type, JSON.parse(readFileSync(path, "utf8")));
}
