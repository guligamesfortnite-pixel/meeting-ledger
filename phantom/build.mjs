// Wraps index.html (written as a page fragment so it can also be published as
// a Claude artifact) in a full HTML document for normal web hosting.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";

const page = readFileSync(new URL("./index.html", import.meta.url), "utf8");
const head =
  '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">' +
  "<style>:root{padding:env(safe-area-inset-top,0px) 0 env(safe-area-inset-bottom,0px)}" +
  "body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style></head><body>";
mkdirSync(new URL("./dist/", import.meta.url), { recursive: true });
writeFileSync(new URL("./dist/index.html", import.meta.url), head + page + "</body></html>");
console.log("built dist/index.html");
