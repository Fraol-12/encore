/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./templates/**/*.js",
    "./templates/**/*.css",
  ],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["Manrope", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      boxShadow: {
        soft: "0 18px 45px -30px rgba(2, 6, 23, 0.45)",
      },
    },
  },
  plugins: [require("@tailwindcss/line-clamp")],
};
