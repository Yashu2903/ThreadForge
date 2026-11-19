/** The usage panel must never show an unpriced cost as $0 (docs/ai.md 22.7). */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { UsageView, costSentence } from "../src/ai/Panels";

const usage = (cost_status: string, cost_micros: number, calls = 3) => ({ cost_status, totals: { calls, cost_micros } });

describe("the usage cost", () => {
  it("is unknown, not zero, when no prices are configured", () => {
    expect(costSentence(usage("unpriced", 0))).toMatch(/cost unknown/);
    expect(costSentence(usage("unpriced", 0))).not.toMatch(/\$0/);
  });
  it("is labelled partial, not a total, when only some calls could be priced", () => {
    const text = costSentence(usage("partial", 1_500_000));
    expect(text).toMatch(/\$1\.5000 so far/);
    expect(text).toMatch(/partial, not a total/);
  });
  it("reads the server's cost_micros field (it used to read a field that does not exist and always showed $0)", () => {
    expect(costSentence(usage("estimated", 250_000))).toBe("about $0.2500");
  });
  it("says no cost for usage of nothing, which is a known zero", () => {
    expect(costSentence(usage("none", 0, 0))).toBe("no cost");
  });
  it("renders in the panel", () => {
    render(<UsageView usage={usage("unpriced", 0)} />);
    expect(screen.getByTestId("usage")).toHaveTextContent("3 calls, cost unknown");
  });
});
