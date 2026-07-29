/**
 * Product identity — the frontend half of the three-file rename surface.
 * Mirrors packages/config/nemonis_config/product.py.
 */

export const PRODUCT_NAME = "Ñemonis";
export const PRODUCT_SLUG = "nemonis";
export const TAGLINE = "Forex research, backtesting and risk-control platform";

export const SAFETY_NOTICE =
  "Research and paper-trading only. This build cannot place real-money orders: no broker adapter exists.";

export const API_URL =
  process.env.NEXT_PUBLIC_NEMONIS_API_URL ?? "http://localhost:8787";
