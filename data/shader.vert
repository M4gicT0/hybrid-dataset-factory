#version 330

uniform mat4 MVP;

in vec3 in_vert;
in vec3 in_norm;
in vec2 in_text;

out vec3 v_vert;
out vec3 v_norm;
out vec2 v_text;

void main() {
	gl_Position = MVP * vec4(in_vert, 1.0);
	v_vert = in_vert;
	v_norm = in_norm;
	v_text = in_text;
}
