/*void main_setup() { // PERRINN front wing in ground effect, full wing in a tight box, moving ground (stl/perrinn_front_wing_fx.stl: metres, flow +y, ground z=0); required extensions in defines.hpp: FP16S, EQUILIBRIUM_BOUNDARIES, MOVING_BOUNDARIES, SUBGRID, INTERACTIVE_GRAPHICS or GRAPHICS
	// ################################################################## define simulation box size, viscosity and volume force ###################################################################
	const float chord = 0.737f; // metres, wing length in the flow direction (span 1.793 m, lowest point 85.6 mm above ground)
	const float box_width = 2.2f; // tunnel width in metres (span 1.793 m + ~0.2 m each side); tunnel length is 1.068x (2.35 m) and height 0.227x (0.5 m) this
	const uint3 lbm_N = resolution(float3(1.0f, 1.068f, 0.227f), 8500u); // input: simulation box aspect ratio and VRAM occupation in MB, output: grid resolution
	const float cells_per_metre = (float)lbm_N.x/box_width;
	const float lbm_u = 0.075f; // inflow speed in lattice units
	const float lbm_Re = 2500000.0f; // Reynolds number based on chord (50 m/s, 0.737 m)
	const ulong lbm_T = 40000ull;
	LBM lbm(lbm_N, 1u, 3u, 1u, units.nu_from_Re(lbm_Re, chord*cells_per_metre, lbm_u)); // 3 domains along y on the same GPU: keeps each memory buffer under the driver's 2559 MB limit
	// ###################################################################################### define geometry ######################################################################################
	const float3 offset = float3(0.5f*(float)lbm_N.x, 0.15f*cells_per_metre, 1.0f); // leading edge 0.15 m from the inlet, span centred, ground height kept (z=0 in the STL -> first cell above the floor)
	Mesh* mesh = read_stl(get_exe_path()+"../stl/perrinn_front_wing_fx.stl", cells_per_metre, float3x3(1.0f), offset);
	lbm.flags.write_to_device();
	lbm.voxelize_mesh_on_device(mesh, TYPE_S);
	lbm.flags.read_from_device();
	delete mesh;
	const uint Nx=lbm.get_Nx(), Ny=lbm.get_Ny(), Nz=lbm.get_Nz(); parallel_for(lbm.get_N(), [&](ulong n) { uint x=0u, y=0u, z=0u; lbm.coordinates(n, x, y, z);
		if(lbm.flags[n]!=TYPE_S) lbm.u.y[n] = lbm_u; // air starts moving at inflow speed
		if(z==0u) { lbm.flags[n] = TYPE_S; lbm.u.y[n] = lbm_u; } // moving ground (solid wall moving at inflow speed)
		else if(x==0u||x==Nx-1u||y==0u||y==Ny-1u||z==Nz-1u) lbm.flags[n] = TYPE_E; // inflow/outflow on all other sides
	}); // ####################################################################### run simulation, export images and data ##########################################################################
	lbm.graphics.visualization_modes = VIS_FLAG_SURFACE|VIS_Q_CRITERION; // wing and floor surfaces, vortices (no VIS_FLAG_LATTICE: that draws the green tunnel-boundary box)
#if defined(GRAPHICS) && !defined(INTERACTIVE_GRAPHICS) // offline video: no window, frames saved to bin/export/
	lbm.graphics.set_camera_centered(-27.9f, 22.0f, 100.0f, 0.778801f); // CAMERA: from pressing G in the interactive window (rotation x, rotation y, field of view, zoom)
	const ulong warmup = 0ull; // 0: record from the very start, so the video shows the wake growing from the wing (25000 = start with the flow already developed)
	const uint frames = 1500u; // video frames: 1500 = 25 seconds at 60 fps
	const ulong steps_per_frame = 15ull; // simulation time steps between frames (20: air moves ~1.5 cells per frame, slow motion)
	lbm.run(warmup);
	for(uint i=0u; i<frames; i++) {
		lbm.run(steps_per_frame);
		lbm.graphics.write_frame(); // bin/export/image-<time step>.png
	}
#else // interactive window
	lbm.run();
#endif // GRAPHICS && !INTERACTIVE_GRAPHICS
} /**/

