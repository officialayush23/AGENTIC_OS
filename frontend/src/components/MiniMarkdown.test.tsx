import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MiniMarkdown, plainText } from "./MiniMarkdown";

describe("MiniMarkdown", () => {
  it("renders lists, bold and http links", () => {
    const html = renderToStaticMarkup(<MiniMarkdown text={"Hi\n\n* **Meeting:** 20 Nov ([Join](https://meet.jit.si/x))\n* Ship UI"} />);
    expect(html).toContain("<ul><li><b>Meeting:</b>");
    expect(html).toContain('<a href="https://meet.jit.si/x"');
  });
  it("never links non-http schemes and escapes html", () => {
    const html = renderToStaticMarkup(<MiniMarkdown text={"[x](javascript:alert(1)) <img src=x>"} />);
    expect(html).not.toContain("href=\"javascript");
    expect(html).toContain("&lt;img");
  });
  it("speaks plain words", () => {
    expect(plainText("**Join** [here](https://a.b/c)")).toBe("Join here");
  });
});
