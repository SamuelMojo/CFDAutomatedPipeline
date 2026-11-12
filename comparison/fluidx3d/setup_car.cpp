/*void main_setup() { // PERRINN F1 car in a wind tunnel with moving ground (one STL per part, made by cfd-pipeline/src/step_to_fluidx3d.py); required extensions in defines.hpp: FP16S, EQUILIBRIUM_BOUNDARIES, MOVING_BOUNDARIES, SUBGRID, INTERACTIVE_GRAPHICS or GRAPHICS
	// ################################################################## define simulation box size, viscosity and volume force ###################################################################
	const float car_length = 5.380f, car_width = 1.988f; // metres (see stl/perrinn_parts/parts.json)
	const float box_width = 5.0f; // tunnel width in metres; tunnel length is 2.8x (14 m) and height 0.55x (2.75 m) this
	const uint3 lbm_N = resolution(float3(1.0f, 2.8f, 0.55f), 8000u); // input: simulation box aspect ratio and VRAM occupation in MB, output: grid resolution
	const float cells_per_metre = (float)lbm_N.x/box_width;
	const float lbm_u = 0.075f; // inflow speed in lattice units
	const float lbm_Re = 1000000.0f; // Reynolds number based on car length
	const ulong lbm_T = 20000ull;
	LBM lbm(lbm_N, 1u, 3u, 1u, units.nu_from_Re(lbm_Re, car_length*cells_per_metre, lbm_u)); // 3 domains along y on the same GPU: keeps each memory buffer under the driver's 2559 MB limit
	// ###################################################################################### define geometry ######################################################################################
	const float3 offset = float3(0.5f*(float)lbm_N.x, 0.25f*(float)lbm_N.y, 1.0f); // car origin (nose tip, centreline, tyre contact) -> lattice position
	vector<uchar> solid(lbm.get_N(), 0u);
	uint parts = 0u;
	for(uint i=0u; ; i++) { // voxelize each part on its own and keep the union, so overlapping parts do not cancel out
		const string path = get_exe_path()+"../stl/perrinn_parts/part_"+to_string(i)+".stl";
		if(!std::ifstream(path).good()) break;
		Mesh* mesh = read_stl(path, cells_per_metre, float3x3(1.0f), offset);
		lbm.flags.write_to_device();
		lbm.voxelize_mesh_on_device(mesh, TYPE_S);
		lbm.flags.read_from_device();
		parallel_for(lbm.get_N(), [&](ulong n) { if(lbm.flags[n]&TYPE_S) solid[n] = 1u; lbm.flags[n] = 0u; });
		delete mesh;
		parts++;
	}
	if(parts==0u) print_error("No parts found in stl/perrinn_parts/. Run cfd-pipeline/src/step_to_fluidx3d.py first.");
	print_info("Voxelized "+to_string(parts)+" parts.");
	const uint Nx=lbm.get_Nx(), Ny=lbm.get_Ny(), Nz=lbm.get_Nz(); parallel_for(lbm.get_N(), [&](ulong n) { uint x=0u, y=0u, z=0u; lbm.coordinates(n, x, y, z);
		if(solid[n]) lbm.flags[n] = TYPE_S; // car
		else lbm.u.y[n] = lbm_u; // air starts moving at inflow speed
		if(z==0u) { lbm.flags[n] = TYPE_S; lbm.u.y[n] = lbm_u; } // moving ground (solid wall moving at inflow speed)
		else if(x==0u||x==Nx-1u||y==0u||y==Ny-1u||z==Nz-1u) lbm.flags[n] = TYPE_E; // inflow/outflow on all other sides
	}); // ####################################################################### run simulation, export images and data ##########################################################################
	lbm.graphics.visualization_modes = VIS_FLAG_SURFACE|VIS_Q_CRITERION;
	lbm.run();
} /**/
