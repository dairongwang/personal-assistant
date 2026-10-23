// Runtime configuration of the ad-admin dashboard (no build step).
//   apiBase: origin of the Account & Sync Service, e.g. "https://api.example.com".
//            "" = same origin (the API serves this dashboard at /ads-admin/ via ADS_ADMIN_DIST).
// In the Docker image this file is generated from the ADS_ADMIN_API_BASE environment variable.
window.ADS_ADMIN_CONFIG = { apiBase: '' };
