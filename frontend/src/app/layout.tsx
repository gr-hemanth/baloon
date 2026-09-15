import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SRM eCurricula Automator",
  description: "Production dashboard for SRM eCurricula worksheet automation",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-slate-900 text-slate-100 min-h-screen font-sans">
        {children}
      </body>
    </html>
  );
}
