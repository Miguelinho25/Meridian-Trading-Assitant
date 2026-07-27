/**
 * Product identity — the frontend half of the three-file rename surface.
 * Mirrors packages/config/meridian_config/product.py.
 */

export const PRODUCT_NAME = "Meridian";
export const PRODUCT_SLUG = "meridian";
export const TAGLINE = "Forex research, backtesting and risk-control platform";

export const SAFETY_NOTICE =
  "Research and paper-trading only. This build cannot place real-money orders: no broker adapter exists.";

export const API_URL =
  process.env.NEXT_PUBLIC_MERIDIAN_API_URL ?? "http://localhost:8787";
