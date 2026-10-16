Validate and repair format only for ONE Confluence Storage XHTML fragment.
Return only XHTML inside one fenced ```xml block, with no JSON wrapper, UI text, or explanation.
Repair serialization, XML escaping, tag nesting, table, CDATA, and macro boundaries only.
If valid, return it unchanged. Never rewrite, summarize, reorder, deduplicate, add, or remove
report content or facts.
