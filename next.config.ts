import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/l/api/:path*",
        destination: "/api/:path*",
      },
    ];
  },
};

export default nextConfig;
