/**/void main_setup() { // PERRINN front wing FORCE MEASUREMENT, to compare with the OpenFOAM job perrinn_fw_sa_half (50 m/s, moving ground, full wing, Aref 1 m^2, downforce = negative Cl as in cfd-pipeline); required extensions in defines.hpp: FP16S, FORCE_FIELD, EQUILIBRIUM_BOUNDARIES, MOVING_BOUNDARIES, SUBGRID (graphics optional: switched off it runs faster)
	// ################################################################## define simulation box size, viscosity and volume force ###################################################################
	const float si_u = 50.0f, si_rho = 1.225f, si_nu = 1.48E-5f; // air at 50 m/s, as in the OpenFOAM job
	const float si_chord = 0.737f, si_aref = 1.0f; // chord, and reference area (cfd-pipeline convention for this wing: full wing, Aref = 1 m^2)
	// run 1 (full wing, 4.0 x 5.0 x 1.5 m box, 6.1 mm cells) -> Cl -0.218, Cd 0.224. Run 2 (this one): half wing as in the OpenFOAM job, same clearances, so the box is half as wide and the cells are finer
	const bool half_model = true; // true: half wing against a mirror wall at the centreline, like the OpenFOAM job (Aref halved, so Cl/Cd are full-wing values); false: full wing
	const float si_box_x = half_model ? 2.0f : 4.0f, si_box_y = 5.0f, si_box_z = 1.5f; // tunnel width, length, height in metres (wing: span 1.79 m, chord 0.74 m, top 0.29 m); 1.1 m beside the endplate, 1.2 m above the wing
	const float si_upstream = 1.5f; // distance from the inlet to the leading edge in metres
	const float si_T = 0.3f, si_T_avg = 0.15f; // simulated time, and the final part of it that is averaged, in seconds (the air crosses the box in 0.1 s)
	const uint memory = 8500u; // VRAM in MB
	const float lbm_u = 0.075f; // inflow speed in lattice units
	const uint3 lbm_N = resolution(float3(si_box_x, si_box_y, si_box_z), memory); // input: simulation box aspect ratio and VRAM occupation in MB, output: grid resolution
	const float cells_per_metre = (float)lbm_N.x/si_box_x;
	units.set_m_kg_s(si_chord*cells_per_metre, lbm_u, 1.0f, si_chord, si_u, si_rho); // chord in cells <-> metres, lattice speed <-> 50 m/s, density 1 <-> 1.225 kg/m^3
	const ulong lbm_T = units.t(si_T), lbm_T_avg = units.t(si_T_avg), lbm_dt = 50ull; // sample the force every 50 time steps
	const float cell_mm = 1000.0f/cells_per_metre;
	print_info("cell size = "+to_string(cell_mm, 2u)+" mm, Re = "+to_string(to_uint(units.si_Re(si_chord, si_u, si_nu)))+", time steps = "+to_string(lbm_T));
	LBM lbm(lbm_N, 1u, 3u, 1u, units.nu(si_nu)); // 3 domains along y on the same GPU: keeps each memory buffer under the driver's 2559 MB limit
	// ###################################################################################### define geometry ######################################################################################
	const float3 offset = float3(half_model ? 0.0f : 0.5f*(float)lbm_N.x, si_upstream*cells_per_metre, 1.0f); // half model: centreline on the x=0 wall, otherwise span centred; leading edge si_upstream from the inlet; ground height kept
	Mesh* mesh = read_stl(get_exe_path()+"../stl/perrinn_front_wing_fx.stl", cells_per_metre, float3x3(1.0f), offset);
	lbm.flags.write_to_device();
	lbm.voxelize_mesh_on_device(mesh, TYPE_S|TYPE_X); // TYPE_X marks the wing, so its force is summed separately from the floor
	lbm.flags.read_from_device();
	delete mesh;
	const uint Nx=lbm.get_Nx(), Ny=lbm.get_Ny(), Nz=lbm.get_Nz(); parallel_for(lbm.get_N(), [&](ulong n) { uint x=0u, y=0u, z=0u; lbm.coordinates(n, x, y, z);
		if(!(lbm.flags[n]&TYPE_S)) lbm.u.y[n] = lbm_u; // air starts at inflow speed (never set a velocity on wing cells: that would make them moving walls)
		if(z==0u||(half_model&&x==0u)) { lbm.flags[n] = TYPE_S; lbm.u.y[n] = lbm_u; } // moving ground; half model: also the centreline wall, moving with the air so it acts close to a symmetry plane (FluidX3D has no symmetry boundary)
		else if(x==0u||x==Nx-1u||y==0u||y==Ny-1u||z==Nz-1u) lbm.flags[n] = TYPE_E; // inflow/outflow on all other sides
	}); // ####################################################################### run simulation, export images and data ##########################################################################
#if defined(GRAPHICS) || defined(INTERACTIVE_GRAPHICS)
	lbm.graphics.visualization_modes = VIS_FLAG_SURFACE|VIS_Q_CRITERION;
#endif // GRAPHICS || INTERACTIVE_GRAPHICS
	const string path = get_exe_path()+(half_model ? "forces_half/" : "forces/"); // bin/forces_half/ or bin/forces/
	write_file(path+"forces.csv", "t_s,side_force_N,drag_N,lift_N,Cd,Cl\n");
	const float q_aref = 0.5f*si_rho*sq(si_u)*(half_model ? 0.5f*si_aref : si_aref); // dynamic pressure x reference area (half model: half the area, as in cfd-pipeline, so Cl/Cd are full-wing values)
	double sum_Cd=0.0, sum_Cl=0.0; float min_Cd=1E9f, max_Cd=-1E9f, min_Cl=1E9f, max_Cl=-1E9f; uint samples=0u;
	Clock clock;
	while(lbm.get_t()<lbm_T) {
		lbm.run(lbm_dt, lbm_T);
		const float3 F = lbm.object_force(TYPE_S|TYPE_X); // force of the air on the wing, lattice units
		const float Fs=units.si_F(F.x), Fd=units.si_F(F.y), Fl=units.si_F(F.z); // side force, drag (flow is +y), lift (+z; downforce is negative)
		const float Cd=Fd/q_aref, Cl=Fl/q_aref;
		write_line(path+"forces.csv", to_string(units.si_t(lbm.get_t()), 5u)+","+to_string(Fs, 2u)+","+to_string(Fd, 2u)+","+to_string(Fl, 2u)+","+to_string(Cd, 4u)+","+to_string(Cl, 4u));
		if(lbm.get_t()>lbm_T-lbm_T_avg) { // averaging window
			sum_Cd += Cd; sum_Cl += Cl; samples++;
			if(Cd<min_Cd) min_Cd=Cd; if(Cd>max_Cd) max_Cd=Cd; if(Cl<min_Cl) min_Cl=Cl; if(Cl>max_Cl) max_Cl=Cl;
		}
		if(lbm.get_t()%(20ull*lbm_dt)==0ull) print_info("t = "+to_string(units.si_t(lbm.get_t()), 4u)+" s   Cd = "+to_string(Cd, 4u)+"   Cl = "+to_string(Cl, 4u));
	}
	const double Cd_mean=sum_Cd/(double)max(samples, 1u), Cl_mean=sum_Cl/(double)max(samples, 1u);
	const string json = string("{\n")+
		"  \"solver\": \"FluidX3D (lattice Boltzmann, D3Q19 SRT, FP16S, Smagorinsky LES, no wall model)\",\n"+
		"  \"case\": \"PERRINN front wing, "+string(half_model ? "HALF wing against a moving mirror wall (Aref 0.5 m^2 for the half)" : "full wing (Aref 1 m^2)")+", moving ground, 50 m/s, coefficients are full-wing values, downforce = negative Cl\",\n"+
		"  \"cell_size_mm\": "+to_string(cell_mm, 3u)+",\n"+
		"  \"grid\": ["+to_string(Nx)+", "+to_string(Ny)+", "+to_string(Nz)+"],\n"+
		"  \"cells\": "+to_string(lbm.get_N())+",\n"+
		"  \"box_m\": ["+to_string(si_box_x, 2u)+", "+to_string(si_box_y, 2u)+", "+to_string(si_box_z, 2u)+"],\n"+
		"  \"reynolds_chord\": "+to_string(to_uint(units.si_Re(si_chord, si_u, si_nu)))+",\n"+
		"  \"simulated_time_s\": "+to_string(si_T, 3u)+",\n"+
		"  \"averaged_over_last_s\": "+to_string(si_T_avg, 3u)+",\n"+
		"  \"samples_averaged\": "+to_string(samples)+",\n"+
		"  \"Cd_mean\": "+to_string(Cd_mean, 4u)+",\n"+
		"  \"Cl_mean\": "+to_string(Cl_mean, 4u)+",\n"+
		"  \"Cd_peak_to_peak\": "+to_string(max_Cd-min_Cd, 4u)+",\n"+
		"  \"Cl_peak_to_peak\": "+to_string(max_Cl-min_Cl, 4u)+",\n"+
		"  \"drag_mean_N_full_wing\": "+to_string(Cd_mean*0.5*si_rho*sq(si_u)*si_aref, 1u)+",\n"+
		"  \"downforce_mean_N_full_wing\": "+to_string(-Cl_mean*0.5*si_rho*sq(si_u)*si_aref, 1u)+",\n"+
		"  \"wall_time_s\": "+to_string(clock.stop(), 0u)+"\n"+
		"}\n";
	write_file(path+"results.json", json);
	print_info("Cd_mean = "+to_string(Cd_mean, 4u)+", Cl_mean = "+to_string(Cl_mean, 4u)+"   written to "+path+"results.json");
} /**/
