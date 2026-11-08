import { type Api, ApiError } from "../api";

/** Upload one image to a room and return the asset; failures become a message the user can act on. */
export async function uploadImage(api: Api, roomId: string, file: File): Promise<{ id: string; width_px: number; height_px: number }> {
  try {
    const res = await api.upload<{ id: string; width_px: number; height_px: number }>(`/rooms/${roomId}/assets`, file, file.type);
    return res.body;
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.code === "rate_limited") throw new Error(`Too many uploads. Try again in ${err.retryAfter ?? 60} seconds.`);
      throw new Error(err.message);
    }
    throw new Error("The image could not be uploaded. Check your connection and try again.");
  }
}
