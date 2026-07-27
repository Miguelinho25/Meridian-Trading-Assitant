import type { Metadata } from "next";
import "./globals.css";
import { PRODUCT_NAME, TAGLINE } from "@/config/product";
import { Providers } from "./providers";
import { Shell } from "./components/Shell";

export const metadata: Metadata = {
  title: `${PRODUCT_NAME} — ${TAGLINE}`,
  description: TAGLINE,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <Providers>
          <Shell>{children}</Shell>
        </Providers>
      </body>
    </html>
  );
}
