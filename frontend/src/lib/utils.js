// Carried from the current app: frontend/src/lib/utils.js at b5d687820e88c10de25a9a2343d3cc478e497524.
import { clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs) {
    return twMerge(clsx(inputs))
}
